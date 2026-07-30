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
#   CREATE_BASE_BRANCH=1           allow creating+pushing BASE_BRANCH when it does
#                                  not exist on the target (default 0 — writing a
#                                  new ref into someone's repo is opt-in)
#   GATE_PROBE=0                   skip the "is TEST_CMD green on a pristine
#                                  BASE_BRANCH?" probe (default: run it)
#   GATE_PROBE_TIMEOUT=<seconds>   bound that probe (default 900)
#
# NOTE: this foreground mode is the dev / first-run path. For real (unattended)
# operation, prefer the systemd units (`issue-agent install-service` /
# `make install-systemd`) — they auto-restart the process itself across a
# suspend/OOM-kill/reboot, which foreground mode (no supervisor) cannot. See
# HARNESS.md "Running for real: systemd".
set -uo pipefail

HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=bin/lib/gate-probe.sh
. "$HARNESS_ROOT/bin/lib/gate-probe.sh"
# shellcheck source=bin/lib/logging.sh
. "$HARNESS_ROOT/bin/lib/logging.sh"
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

# A configured TEST_CMD belongs to the target checkout. The deployment knobs
# that selected it must not be inherited by its descendants; otherwise this
# project self-hosting `make test` recursively invokes its own gate hooks.
# HARNESS_GATE_DEPTH is the re-entrancy bound backing that scrub (see
# plugin/hooks/checks/_pr_common.sh:prc_run_test_cmd for the full rationale):
# executing at depth ≥ 2 means the command re-entered the gate (#63) — refuse.
# Exported: the probe transports run_target_test_cmd into a `timeout bash -c`
# subshell via `declare -f`, which does NOT carry plain script variables.
export RTC_SCRUB_STATIC="TEST_CMD LINT_CMD TYPECHECK_CMD EDIT_CHECK_CMD ALLOW_UNTESTED RED_MUST_FAIL VERIFY_RED_AT_COMMIT BASE_BRANCH GATE_TEST_TIMEOUT_S GATE_TEST_FAILURE_EXIT_CODES GATE_GH_RETRY_SLEEP HARNESS_SCREENSHOTS_READ GH_APP_ID GH_APP_KEY_FILE GH_APP_INSTALL_ID TARGET_DIR AGENT_ENV"

run_target_test_cmd() {  # run_target_test_cmd "<command>"
  _rtc_depth="${HARNESS_GATE_DEPTH:-0}"
  case "$_rtc_depth" in ''|*[!0-9]*) _rtc_depth=0 ;; esac
  if [ "$_rtc_depth" -ge 2 ]; then
    echo "GATE RECURSION: refusing to run a test command at gate depth $_rtc_depth (see #63): $1" >&2
    return 1
  fi
  _rtc_scrub=()
  for _rtc_v in $RTC_SCRUB_STATIC ${HARNESS_DEPLOY_KNOBS:-} HARNESS_DEPLOY_KNOBS; do
    case "$_rtc_v" in [A-Za-z_]*) _rtc_scrub+=(-u "$_rtc_v") ;; esac
  done
  env "${_rtc_scrub[@]}" \
    HARNESS_GATE_DEPTH="$((_rtc_depth + 1))" \
    bash -c "$1"
}

[ -f "$SHARED" ]   || { red "missing $SHARED";   exit 1; }
[ -f "$REPO_ENV" ] || { red "missing $REPO_ENV (create config/repos/$NAME.env)"; exit 1; }

mkdir -p "$LOG_DIR" || { red "could not create log dir $LOG_DIR"; exit 1; }
chmod 700 "$(dirname "$LOG_DIR")" 2>/dev/null || true

# ── 1. Compose: shared first, per-repo overlays (per-repo wins) ───────────────
# Delegated to bin/compose-env.sh (the single source of truth for the compose
# step, shared with the `issue-agent` CLI). Sourced (not executed) so REPO /
# TARGET_DIR / BASE_BRANCH / ROLES-relevant vars land in THIS shell for the steps
# below; it also writes config/.active.<name>.env and exports AGENT_ENV to it.
info "composing env: shared.env + repos/$NAME.env → config/.active.$NAME.env"
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
    # The repo's REAL default branch, per GitHub. Deliberately NOT the local
    # `refs/remotes/origin/HEAD` symbolic ref this used to read: that ref is a
    # cache written at clone time and never refreshed, so a repo that has since
    # changed its default branch reports the OLD one forever. Observed on
    # xnmp/keifu (2026-07-28): origin/HEAD still said `main` while GitHub's
    # default_branch had long been `chong-dev`.
    default="$(gh api "repos/$REPO" --jq '.default_branch' 2>/dev/null)"
    if [ -z "$default" ]; then
      default="$(git -C "$TARGET_DIR" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#origin/##')"
      default="${default:-main}"
      warn_default=" (could not reach the GitHub API; fell back to the local, possibly stale origin/HEAD)"
    else
      warn_default=""
    fi
    # Creating this branch PUSHES a new ref into someone's repository. That is
    # not a reasonable thing to do silently as a side effect of "start the
    # daemons" — especially when BASE_BRANCH is simply the un-overridden `dev`
    # default and the repo's own default branch is something else entirely.
    # Require an explicit opt-in and say exactly what the alternative is.
    if [ "${CREATE_BASE_BRANCH:-0}" = "1" ]; then
      info "  creating '$BASE_BRANCH' off origin/$default$warn_default (CREATE_BASE_BRANCH=1)"
      git -C "$TARGET_DIR" fetch origin "$default" --quiet \
        && git -C "$TARGET_DIR" branch "$BASE_BRANCH" "origin/$default" 2>/dev/null \
        && uv run --quiet issue-agent publish-base-branch \
             --repo "$REPO" --name "$NAME" --target-dir "$TARGET_DIR" \
             --branch "$BASE_BRANCH" \
        && grn "  ✓ created and pushed '$BASE_BRANCH'" \
        || { red "  ✗ could not create '$BASE_BRANCH' (check push access to $REPO)"; exit 1; }
    else
      red "  ✗ BASE_BRANCH='$BASE_BRANCH' does not exist on $REPO."
      red "    That repo's default branch is '$default'$warn_default."
      if [ "$BASE_BRANCH" = "$default" ]; then
        red "    Creating it would be a no-op mismatch — check the remote is reachable."
      else
        red "    Did you mean BASE_BRANCH='$default'? Set it in config/repos/<name>.env."
      fi
      red "    To create and push '$BASE_BRANCH' off origin/$default anyway, re-run with"
      red "    CREATE_BASE_BRANCH=1. (This writes a new branch to $REPO, so it is opt-in.)"
      exit 1
    fi
  fi
else
  red "TARGET_DIR ('${TARGET_DIR:-}') is not a git working copy — cannot ensure the base branch."
  exit 1
fi

# ── 3b. Gate probe: is TEST_CMD green on an untouched BASE_BRANCH? ────────────
# doctor only ever ECHOED TEST_CMD. Nothing ran it, so a base branch on which
# the command is already red sailed through preflight — and every resolve then
# burned a full session discovering that its PR gate could never go green. On
# xnmp/keifu the tree had never been `cargo fmt`-ed, so a TEST_CMD mirroring CI
# was red on a pristine checkout; that is a 41-second discovery here versus a
# ~$20 one after the fact.
#
# Probed in a THROWAWAY WORKTREE of origin/$BASE_BRANCH, never in TARGET_DIR:
# the operator's checkout routinely carries local edits and untracked files, and
# "green with your junk in the tree" is not the question. Keep the throwaway
# worktree under TARGET_DIR, matching the daemon worktrees: package managers can
# then resolve checkout-local dependency stores such as an ancestor
# `node_modules`. A /tmp sibling made every JS probe falsely red (#49).
#
# GATE_PROBE=0 skips it; GATE_PROBE_TIMEOUT bounds it (default 900s).
if [ "${GATE_PROBE:-1}" = "1" ] && [ -n "${TEST_CMD:-}" ]; then
  info "gate probe: running TEST_CMD on a clean origin/$BASE_BRANCH worktree"
  if ! _probe_dir="$(
    create_gate_probe_worktree_dir \
      "$TARGET_DIR" "${WORKTREE_ROOT-.claude/worktrees}"
  )"; then
    red "  ✗ could not create gate-probe worktree below WORKTREE_ROOT"
    exit 1
  fi
  # The log lives OUTSIDE the probe worktree: `git worktree remove --force`
  # deletes that directory, taking the evidence with it. Its own mktemp name
  # also keeps concurrent deployments from overwriting each other's output.
  _probe_log="$(mktemp "${TMPDIR:-/tmp}/issue-agent-gate-probe.XXXXXX.log")"
  _probe_rc=0
  git -C "$TARGET_DIR" fetch origin "$BASE_BRANCH" --quiet || true
  if git -C "$TARGET_DIR" worktree add --detach --quiet "$_probe_dir" "origin/$BASE_BRANCH" 2>/dev/null; then
    # `timeout` is coreutils and not guaranteed present; degrade to unbounded
    # rather than skipping the probe (an unbounded green still answers the
    # question, and Ctrl-C is available).
    if command -v timeout >/dev/null 2>&1; then
      ( cd "$_probe_dir" && timeout "${GATE_PROBE_TIMEOUT:-900}" bash -c "$(declare -f run_target_test_cmd); run_target_test_cmd \"\$1\"" _ "$TEST_CMD" ) >"$_probe_log" 2>&1 || _probe_rc=$?
    else
      ( cd "$_probe_dir" && run_target_test_cmd "$TEST_CMD" ) >"$_probe_log" 2>&1 || _probe_rc=$?
    fi
    git -C "$TARGET_DIR" worktree remove --force "$_probe_dir" 2>/dev/null || true
    if [ "$_probe_rc" -eq 0 ]; then
      grn "  ✓ TEST_CMD is green on a pristine origin/$BASE_BRANCH"
      rm -f "$_probe_log" 2>/dev/null || true
    else
      if [ "$_probe_rc" -eq 124 ]; then
        red "  ✗ TEST_CMD TIMED OUT (>${GATE_PROBE_TIMEOUT:-900}s) on a pristine origin/$BASE_BRANCH."
        red "    The gate runs this command on every commit and every push — a command"
        red "    this slow will time the agent out too. Raise GATE_PROBE_TIMEOUT only if"
        red "    you are sure the gate's own timeout can absorb it."
      else
        red "  ✗ TEST_CMD FAILED (exit $_probe_rc) on a pristine origin/$BASE_BRANCH."
        red "    The base branch is already red, so no PR the harness opens can ever"
        red "    reach a green gate — every resolve will burn a full session and fail."
        red "    Fix the base branch, or narrow TEST_CMD to the tiers that pass."
      fi
      red "    Output: $_probe_log"
      red "    Command: $TEST_CMD"
      red "    Override with GATE_PROBE=0 if you know better."
      exit 1
    fi
  else
    red "  ✗ could not create a probe worktree from origin/$BASE_BRANCH — is the branch fetched?"
    rmdir "$_probe_dir" 2>/dev/null || true
    rm -f "$_probe_log" 2>/dev/null || true
    exit 1
  fi
elif [ -z "${TEST_CMD:-}" ]; then
  info "gate probe skipped (TEST_CMD unset — doctor already warned)"
else
  info "gate probe skipped (GATE_PROBE=0)"
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
if ! uv run --quiet issue-agent prepare-remote --repo "$REPO" --name "$NAME"; then
  red "  ✗ lifecycle label preparation failed; refusing to start without the remote command receipts"
  exit 1
fi
grn "  ✓ labels ensured"

# Enable GitHub native auto-merge on the target so an approved+green PR merges
# itself (the harness's merge leg uses `gh pr merge --auto`, which the repo must
# permit — otherwise it loops "Auto merge is not allowed"). Default on; set
# ENSURE_AUTO_MERGE=0 to skip (e.g. a repo whose policy forbids it). Idempotent;
# needs admin on the repo — a non-admin failure is a warning, not fatal.
if [ "${ENSURE_AUTO_MERGE:-1}" != "0" ]; then
  info "ensuring GitHub auto-merge is enabled on $REPO"
  if uv run --quiet issue-agent prepare-remote --repo "$REPO" --name "$NAME" \
      --skip-labels --ensure-auto-merge; then
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
add_exclude "AGENTS.override.md"
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
  : > "$log"
  # Run uv from the HARNESS root (that's where the issue_agent package + project
  # live). The daemon self-chdirs into TARGET_DIR (config.py os.chdir(target_dir)),
  # so git/worktree ops still land in the target repo.
  #
  # Hot-reload relaunch loop: the daemon exits 87 (adapters/reload.py
  # RELOAD_EXIT_CODE) when the harness source changed — always BETWEEN ticks,
  # never mid-session — and we relaunch it on the new code. Going back through
  # `uv run` also picks up dependency changes, and re-sourcing the composed env
  # file lets config edits ride along (touch it, or edit + `touch`, to apply;
  # regenerating the composition still needs a point-at rerun). Any other exit
  # status is a real death and propagates as before.
  (
    cd "$HARNESS_ROOT"
    reloaded=0
    while :; do
      rc=0
      uv run --extra sdk --with tree-sitter --with tree-sitter-bash \
        python -m issue_agent.app.main "$role" || rc=$?
      if [ "$rc" -eq 87 ]; then
        if [ -n "${AGENT_ENV:-}" ] && [ -f "$AGENT_ENV" ]; then
          set -a; . "$AGENT_ENV"; set +a
        fi
        echo "hot-reload: relaunching $role on changed source (exit 87)"
        reloaded=1
        continue
      fi
      # A crash AFTER a hot reload is most likely a bad commit (observed
      # 2026-07-27: a mid-edit mtime reload booted a half-changed tree and
      # TypeError'd; fix+review stayed dead while point-at ran on). Don't die:
      # crash-loop slowly — the commit that FIXES the crash is itself picked
      # up by the next boot's reload watch, so this self-heals. A crash on
      # the FIRST boot (never reloaded) still propagates: that's a config
      # error a retry loop would only hide.
      [ "$reloaded" -eq 1 ] || exit "$rc"
      echo "hot-reload: $role crashed post-reload (exit $rc) — retrying in 60s (fix by committing; the retry picks it up)"
      sleep 60
    done
  ) 2>&1 | timestamp_lines >>"$log" &
}

# Webhook push (#1): a fourth supervised process. `gh webhook forward` streams
# deliveries; the forwarder turns each one into a wake-file touch per role, so
# the daemons notice work in ~a second instead of at the reconcile interval.
# Deliveries are wake signals only — nothing reads the payload — so a dead
# forwarder is a latency regression, never a correctness one: the daemons keep
# reconciling at RECONCILE_SECONDS. Restarted with a 60s backoff because the
# common death is structural (extension not installed, token without
# admin:repo_hook) and a tight relaunch loop would only spin on it.
run_webhook() {
  local log="$LOG_DIR/webhook-$NAME.log"
  info "starting webhook forwarder → $log"
  : > "$log"
  (
    cd "$HARNESS_ROOT"
    while :; do
      uv run --extra sdk --with tree-sitter --with tree-sitter-bash \
        python -m issue_agent.app.webhook || true
      echo "webhook forwarder died — retrying in 60s (daemons stay on the reconcile cadence)"
      sleep 60
    done
  ) 2>&1 | timestamp_lines >>"$log" &
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

# After the watcher check on purpose: the forwarder is an accelerator, not a
# watcher — it must never make "no watchers started" look like a healthy run.
# Its pid is deliberately NOT in pids: the supervising subshell is an infinite
# relaunch loop, so waiting on it would keep point-at alive forever after every
# actual watcher exited (bounded ISSUE_AGENT_MAX_TICKS runs would never end).
webhook_pid=""
case "$(printf '%s' "${WEBHOOK_WAKE:-}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) run_webhook; webhook_pid="$!" ;;
esac
grn "✓ $ROLES watchers running against $REPO (Ctrl-C stops all). Tailing logs from $LOG_DIR:"
echo
# Follow all role logs together until the daemons exit or Ctrl-C.
tail -n +1 -F "$LOG_DIR"/*-"$NAME".log &
tail_pid=$!
wait "${pids[@]}"
# TERM on the supervising subshell alone would orphan its uv→python→gh chain
# (a headless forwarder touching wake files and holding a relay hook), so
# sweep the whole descendant tree. Ctrl-C never gets here — the trap's
# `kill 0` takes the process group down as one.
kill_tree() {
  local child
  for child in $(pgrep -P "$1" 2>/dev/null); do kill_tree "$child"; done
  kill "$1" 2>/dev/null || true
}
[ -n "$webhook_pid" ] && kill_tree "$webhook_pid"
kill "$tail_pid" 2>/dev/null || true
