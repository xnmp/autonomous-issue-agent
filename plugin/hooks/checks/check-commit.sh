#!/usr/bin/env bash
# Governed: `git commit …`. Receives the commit-onward tokens as argv.
#
# Fast gates ONLY (this runs synchronously inside a PreToolUse hook):
#   1. Commit-message prefix must be one of the allowed TDD/maintenance prefixes.
#   2. Optional project lint + typecheck (env LINT_CMD / TYPECHECK_CMD).
# Tests are deliberately NOT run here — they belong in CI (a blocking commit
# hook that runs the suite would time out and serialise the agent).
#
# Empty/file-based messages (-F file, -F- heredoc) can't be prefix-validated
# here; we allow them and let the PR gate re-derive truth from the git log.
set -uo pipefail

# Accept BOTH the bare harness prefixes (`red: `) AND a conventional commit that
# carries the phase as a scope (`test(red): `). The scoped form is for targets with
# a commitlint commit-msg hook (@commitlint/config-conventional) that REJECTS a bare
# `red:` — a `(red)` scope on a legal type passes both. Kept byte-for-byte in sync
# with domain/gate.py VALID_COMMIT_PREFIX + commit_phase().
VALID_PREFIX='^(plan|red|green|evidence|docs|chore|refactor|test|fix|feat|build|ci|perf|revert|style)(\([^)]*\))?: '
# Phase (red/green/plan/evidence) from a bare or conventional-scoped subject; '' if none.
commit_phase() {
  case "$1" in
    plan:*|red:*|green:*|evidence:*) echo "${1%%:*}" ;;
    *\(red\):*)      echo red ;;
    *\(green\):*)    echo green ;;
    *\(plan\):*)     echo plan ;;
    *\(evidence\):*) echo evidence ;;
    *) echo "" ;;
  esac
}

msg=""
for ((i = 1; i <= $#; i++)); do
  case "${!i}" in
    -m | --message)
      j=$((i + 1)); msg="${!j:-}"; break ;;
    -m*)
      msg="${!i#-m}"; break ;;
    --message=*)
      msg="${!i#--message=}"; break ;;
  esac
done

if [ -n "$msg" ] && ! [[ "$msg" =~ $VALID_PREFIX ]]; then
  echo "Commit message must start with an allowed prefix:"
  echo "  plan: red: green: evidence: docs: chore: refactor: test: fix: feat:"
  echo "or, for a repo whose commitlint forbids those, a conventional type with the"
  echo "phase as a scope, e.g. test(red): / feat(green):"
  echo "Got: $msg"
  exit 1
fi

# Fast gates only. Projects supply these via env (set in AGENT.md / repo CLAUDE.md).
if [ -n "${LINT_CMD:-}" ]; then
  eval "$LINT_CMD" || { echo "Lint failed (LINT_CMD): $LINT_CMD"; exit 1; }
fi
if [ -n "${TYPECHECK_CMD:-}" ]; then
  eval "$TYPECHECK_CMD" || { echo "Typecheck failed (TYPECHECK_CMD): $TYPECHECK_CMD"; exit 1; }
fi

# Verify-red-at-commit: a `red:` commit's tests must ACTUALLY fail (run on the
# working tree now). The §10 red-fail check at PR time is the committed-state
# backstop; this is the earlier, in-session fail-fast. On by default; set
# VERIFY_RED_AT_COMMIT=0 to disable (e.g. a suite too heavy to run in a commit hook).
if [ "${VERIFY_RED_AT_COMMIT:-1}" != "0" ] && [ "$(commit_phase "$msg")" = "red" ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # shellcheck source=/dev/null
  [ -f "$HERE/_pr_common.sh" ] && . "$HERE/_pr_common.sh"
  if declare -f prc_resolve_test_cmd >/dev/null; then
    _tc="$(prc_resolve_test_cmd)"
    if [ -n "$_tc" ]; then
      if eval "$_tc" >/dev/null 2>&1; then
        echo "'red:' commit but the test suite PASSES ($_tc) — the new test is not"
        echo "actually failing. Write the FAILING test first, before any implementation."
        exit 1
      fi
    fi
  fi
fi
exit 0
