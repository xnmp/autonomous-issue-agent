#!/usr/bin/env bash
# Focused regression checks for point-at's operator-facing shell workflow.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=bin/lib/gate-probe.sh
. "$ROOT/bin/lib/gate-probe.sh"
# shellcheck source=bin/lib/logging.sh
. "$ROOT/bin/lib/logging.sh"
# shellcheck source=bin/lib/harness-cli.sh
. "$ROOT/bin/lib/harness-cli.sh"
PASS=0
FAIL=0

ok() {
  PASS=$((PASS + 1))
  printf '  \033[32mok\033[0m   %s\n' "$1"
}

no() {
  FAIL=$((FAIL + 1))
  printf '  \033[31mFAIL\033[0m %s\n' "$1"
  [ -n "${2:-}" ] && printf '       %s\n' "$2"
}

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
harness_project="$scratch/harness project"
target_project="$scratch/target project"
fake_bin="$scratch/bin"
mkdir -p "$harness_project" "$target_project" "$fake_bin"
cat >"$fake_bin/uv" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$PWD" >"$HARNESS_CLI_TEST_PWD"
printf '%s\n' "$*" >"$HARNESS_CLI_TEST_ARGS"
EOF
chmod +x "$fake_bin/uv"
HARNESS_ROOT="$harness_project"
HARNESS_CLI_TEST_PWD="$scratch/cli-pwd"
HARNESS_CLI_TEST_ARGS="$scratch/cli-args"
export HARNESS_ROOT HARNESS_CLI_TEST_PWD HARNESS_CLI_TEST_ARGS
if (cd "$target_project" && PATH="$fake_bin:$PATH" \
    run_harness_issue_agent prepare-remote --repo o/r); then
  cli_pwd="$(cat "$HARNESS_CLI_TEST_PWD")"
  cli_args="$(cat "$HARNESS_CLI_TEST_ARGS")"
else
  cli_pwd=""
  cli_args=""
fi
if [ "$cli_pwd" = "$harness_project" ] \
  && [ "$cli_args" = "run --quiet issue-agent prepare-remote --repo o/r" ]; then
  ok "nested CLI commands resolve from the harness project"
else
  no "nested CLI commands resolve from the harness project" \
    "pwd=${cli_pwd:-<missing>} args=${cli_args:-<missing>}"
fi
HARNESS_ROOT="$ROOT"
export HARNESS_ROOT

timestamped="$(printf 'one\ntwo\n' | timestamp_lines)"
timestamp_line_one="${timestamped%%$'\n'*}"
timestamp_line_two="${timestamped#*$'\n'}"
if [[ "$timestamp_line_one" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\ one$ ]] \
  && [[ "$timestamp_line_two" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\ two$ ]]; then
  ok "role log lines receive UTC ISO timestamps"
else
  no "role log lines receive UTC ISO timestamps" "$timestamped"
fi

target="$scratch/target with spaces"
mkdir -p "$target/node_modules"
touch "$target/node_modules/checkout-local-dependency"

probe="$(create_gate_probe_worktree_dir "$target")"
case "$probe" in
  "$target"/.claude/worktrees/gate-probe.*)
    ok "gate probe is nested below TARGET_DIR like issue worktrees"
    ;;
  *)
    no "gate probe is nested below TARGET_DIR like issue worktrees" "path=$probe"
    ;;
esac

find_ancestor_dependency() {
  local start="$1"
  local dependency="$2"
  local cursor

  cd "$start" || return 1
  cursor="$PWD"
  while [ "$cursor" != "/" ]; do
    if [ -f "$cursor/node_modules/$dependency" ]; then
      printf '%s\n' "$cursor/node_modules/$dependency"
      return 0
    fi
    cursor="${cursor%/*}"
    [ -n "$cursor" ] || cursor="/"
  done
  return 1
}

resolved="$(find_ancestor_dependency "$probe" checkout-local-dependency)"
if [ "$resolved" = "$target/node_modules/checkout-local-dependency" ]; then
  ok "probe commands can resolve checkout-local dependencies through ancestors"
else
  no "probe commands can resolve checkout-local dependencies through ancestors" \
    "resolved=${resolved:-<missing>}"
fi

second_probe="$(create_gate_probe_worktree_dir "$target")"
if [ "$second_probe" != "$probe" ] && [ -d "$second_probe" ]; then
  ok "concurrent gate probes receive unique worktree directories"
else
  no "concurrent gate probes receive unique worktree directories"
fi

configured_target="$scratch/configured target"
git init -q -b dev "$configured_target"
git -C "$configured_target" config user.email test@example.com
git -C "$configured_target" config user.name test
git -C "$configured_target" config commit.gpgsign false
touch "$configured_target/seed"
git -C "$configured_target" add seed
git -C "$configured_target" -c core.hooksPath=/dev/null commit -qm seed
mkdir -p "$configured_target/frontend/node_modules"
touch "$configured_target/frontend/node_modules/configured-dependency"

configured_probe="$(
  create_gate_probe_worktree_dir "$configured_target" "frontend/.worktrees"
)"
git -C "$configured_target" worktree add --detach --quiet "$configured_probe" HEAD
configured_resolved="$(
  find_ancestor_dependency "$configured_probe" configured-dependency
)"
if [ "$configured_resolved" = \
  "$configured_target/frontend/node_modules/configured-dependency" ]; then
  ok "configured relative WORKTREE_ROOT matches real issue-worktree resolution"
else
  no "configured relative WORKTREE_ROOT matches real issue-worktree resolution" \
    "resolved=${configured_resolved:-<missing>}"
fi
git -C "$configured_target" worktree remove --force "$configured_probe"

absolute_root="$scratch/absolute worktrees"
absolute_probe="$(
  create_gate_probe_worktree_dir "$configured_target" "$absolute_root"
)"
case "$absolute_probe" in
  "$absolute_root"/gate-probe.*)
    ok "absolute WORKTREE_ROOT keeps the runtime's absolute placement"
    ;;
  *)
    no "absolute WORKTREE_ROOT keeps the runtime's absolute placement" \
      "path=$absolute_probe"
    ;;
esac

if create_gate_probe_worktree_dir "$scratch/missing" >/dev/null 2>&1; then
  no "missing target checkout is rejected"
else
  ok "missing target checkout is rejected"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
