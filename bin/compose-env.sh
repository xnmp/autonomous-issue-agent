#!/usr/bin/env bash
# Compose config/shared.env + config/repos/<name>.env → config/.active.<name>.env.
#
# Single source of truth for the env-compose step, so BOTH bin/point-at.sh (which
# SOURCES this to keep REPO/TARGET_DIR/… in its own shell) and the `issue-agent`
# CLI (which EXECUTES it just to produce the file) see one identical, expanded
# .active.<name>.env (PER-NAME, so two deployments never clobber each other's
# composed file or trip each other's mtime reload watch). Sourcing shared FIRST
# then the per-repo overlay means per-repo
# values win. Snapshots the resolved (expanded) values so the Python daemons (via
# the exported os.environ) and the bash tools (via AGENT_ENV) read the same thing.
#
#   . bin/compose-env.sh <name>     # sourced: also exports AGENT_ENV=.active.env
#   bash bin/compose-env.sh <name>  # executed: just writes .active.env
set -uo pipefail

# Works whether sourced (BASH_SOURCE[0] = this file) or executed.
_CE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_CE_NAME="${1:-}"
_CE_SHARED="$_CE_ROOT/config/shared.env"
_CE_REPO_ENV="$_CE_ROOT/config/repos/$_CE_NAME.env"
_CE_ACTIVE="$_CE_ROOT/config/.active.$_CE_NAME.env"

# Fail loud on a missing name/file instead of silently composing a partial
# .active.env. `_ce_fail` only prints — it must NOT also carry the return/exit,
# because it is a FUNCTION: bash's `return` inside a function only unwinds that
# function's own frame, never the sourcing script that called it (confirmed by
# hand: `f(){ return 1; }; f; echo still-here` prints "still-here" when
# sourced). The prior one-liner that combined message + `return 1 2>/dev/null
# || exit 1` inside `_ce_fail` therefore never stopped compose-env.sh at all —
# a missing overlay printed the error and then kept going, composing a bogus
# .active.env from whatever partially sourced. `$_CE_ABORT` is expanded and
# invoked INLINE at each call site (not through a function), which does abort
# correctly in both modes: `return` for `. compose-env.sh` (stops this file,
# doesn't kill the caller's shell), `exit` for `bash compose-env.sh`.
_ce_fail() { echo "compose-env: $1" >&2; }
if (return 0 2>/dev/null); then _CE_ABORT=return; else _CE_ABORT=exit; fi

[ -n "$_CE_NAME" ]      || { _ce_fail "usage: compose-env.sh <name>"; $_CE_ABORT 1; }
[ -f "$_CE_SHARED" ]    || { _ce_fail "missing $_CE_SHARED"; $_CE_ABORT 1; }
[ -f "$_CE_REPO_ENV" ]  || { _ce_fail "missing $_CE_REPO_ENV (create config/repos/$_CE_NAME.env)"; $_CE_ABORT 1; }

# review C5(a): this used to emit a hand-maintained allow-list of ~40 vars that
# omitted ~18 documented knobs (MAX_CI_RETRIES, CI_IGNORE_CHECKS, WORKTREE_ROOT,
# FIX_MCP_CONFIG, ... — see review C5 for the full list), so an operator setting
# any of them under systemd was silently ignored. Fix: snapshot the environment
# before sourcing, snapshot it again after, and emit every var either file
# defined or changed — the same env -0 / diff technique `_load_agent_env` (in
# src/issue_agent/app/main.py) uses to load AGENT_ENV. This can never omit a
# knob added to shared.env/repos/*.env without a matching compose-env.sh edit.
#
# `env -0` is NUL-delimited (values may legitimately contain newlines), and
# command substitution `$(...)` silently drops NUL bytes — so the snapshots are
# read via process substitution + a `read -d ''` loop, never `$(env -0)`.
declare -A _CE_PRE=()
while IFS= read -r -d '' _ce_kv; do
  [[ "$_ce_kv" == *=* ]] || continue
  _CE_PRE["${_ce_kv%%=*}"]="${_ce_kv#*=}"
done < <(env -0)

# review P0R-1 (regression introduced by the C5(a) fix above): the PRE/POST diff
# alone emits a var only when it is NEW or CHANGED. A var the two files DO
# assign, to the value the launching shell already exported, is byte-identical
# in both snapshots and was therefore silently dropped. The triggers are
# everyday: `FOO="${FOO:-default}"` (the parameter-expansion idiom main.py's
# `_load_agent_env` docstring cites as the reason these files are shell syntax)
# is a no-op assignment whenever FOO is already exported, and TEST_CMD commonly
# matches the operator's own exported value. Under systemd (units pass only
# AGENT_ENV + TARGET_DIR) the daemon then saw TEST_CMD=None, the bash gate
# checkers failed closed, and every push / `gh pr create` was denied forever —
# persisting across reboots, since install-systemd.sh snapshots the file.
#
# Fix: emit the UNION of (a) the diff — which still covers vars set indirectly,
# e.g. by a function or a nested `source`, that no assignment line names — and
# (b) every variable NAME the two files textually assign. C5(a)'s property is
# preserved: (b) is derived from the files themselves, so there is still no
# hand-maintained allow-list to fall out of sync with a newly added knob.
#
# The anchor `^[[:space:]]*` (no `#` allowed) means a commented-out `# FOO=bar`
# is NOT picked up; `export FOO=` and leading whitespace are. A name scanned
# here but NOT set in POST (later `unset`, or a conditional assignment that
# never fired) is never emitted as empty — the emit loop iterates the POST
# snapshot, so membership in POST is a precondition, not an afterthought.
declare -A _CE_ASSIGNED=()
while IFS= read -r _ce_name; do
  [ -n "$_ce_name" ] || continue
  _CE_ASSIGNED["$_ce_name"]=1
done < <(
  grep -hoE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' \
    "$_CE_SHARED" "$_CE_REPO_ENV" \
    | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' \
    | sort -u
)

set -a
# shellcheck disable=SC1090
. "$_CE_SHARED"
# shellcheck disable=SC1090
. "$_CE_REPO_ENV"
set +a

declare -A _CE_POST=()
while IFS= read -r -d '' _ce_kv; do
  [[ "$_ce_kv" == *=* ]] || continue
  _CE_POST["${_ce_kv%%=*}"]="${_ce_kv#*=}"
done < <(env -0)

# Vars the shell itself owns (PATH, PWD, SHLVL, _, ...) are untouched by
# sourcing two KEY=VALUE files: they are identical in _CE_PRE/_CE_POST AND the
# files never name them, so neither union arm selects them — no denylist
# needed. A var an overlay DOES deliberately change (even a shell-owned one,
# e.g. PATH) is emitted by either arm: the overlay author asked for it.
{
  echo "# AUTO-GENERATED by bin/compose-env.sh from shared.env + repos/$_CE_NAME.env — do not edit."
  for _ce_v in "${!_CE_POST[@]}"; do
    # `set -a` exports FUNCTIONS too, so a helper defined in shared.env lands
    # in `env -0` as `BASH_FUNC_name%%=() {...}` — a name that is not a valid
    # shell identifier. Emitting it produced an .active.env that neither
    # `_load_agent_env` nor the bash tools can source cleanly (the line parses
    # as a command, not an assignment: "BASH_FUNC_name%%=...: command not
    # found"). Every real knob is an identifier, so this can't drop config.
    [[ "$_ce_v" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ "${_CE_ASSIGNED[$_ce_v]+set}" == "set" \
       || "${_CE_PRE[$_ce_v]+set}" != "set" \
       || "${_CE_POST[$_ce_v]}" != "${_CE_PRE[$_ce_v]}" ]]; then
      printf '%s=%q\n' "$_ce_v" "${_CE_POST[$_ce_v]}"
    fi
  done | sort
} > "$_CE_ACTIVE"

# Manifest of every knob this deployment sets (#63): the gate wrappers scrub
# these names — plus a static floor — from every target-command child env, so
# a knob added to shared.env or an overlay can never leak into a target's
# tests without anyone maintaining a denylist.
_ce_knobs="$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*' "$_CE_ACTIVE" | grep -v '^HARNESS_DEPLOY_KNOBS$' | sort | tr '\n' ' ')"
printf 'HARNESS_DEPLOY_KNOBS=%q\n' "${_ce_knobs% }" >> "$_CE_ACTIVE"
# Export into the CURRENT shell too: point-at's gate probe scrubs via this var
# and does not source the active file it just wrote (the daemons do).
export HARNESS_DEPLOY_KNOBS="${_ce_knobs% }"
unset _ce_knobs

unset _CE_PRE _CE_POST _CE_ASSIGNED _ce_kv _ce_v _ce_name _CE_ABORT

export AGENT_ENV="$_CE_ACTIVE"
