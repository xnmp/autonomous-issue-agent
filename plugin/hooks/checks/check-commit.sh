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

# Harness-generated project instructions must never enter the target's history.
# Local excludes do not hide a tracked AGENTS*.md, so inspect the staged blob
# itself; target-owned instruction edits remain allowed when they lack our marker.
for instructions_file in AGENTS.md AGENTS.override.md; do
  git diff --cached --name-only --diff-filter=ACMR 2>/dev/null \
    | grep -qxF "$instructions_file" || continue
  if git show ":$instructions_file" 2>/dev/null \
      | grep -qF '<!-- issue-agent generated instructions: start -->'; then
    echo "Blocked: $instructions_file contains issue-agent generated instructions."
    echo "Restore the target-owned file; harness scaffolding must not be committed."
    exit 1
  fi
done

# Oversized-image gate. The review agent READS evidence images, and a file whose
# base64 exceeds the SDK's 1MB message buffer kills the review transport
# deterministically (harness #13; observed live 2026-07-27, PR #552's ~2.3MB
# screenshots burned review retries on an unwinnable run). Enforce at commit
# time — where the file is created and the author can still re-capture. Checks
# files already staged AND images sitting in evidence/: this hook fires BEFORE
# the command executes, so a compound `git add … && git commit` has staged
# nothing yet. 700KB default keeps base64 (×4/3) under the 1MB buffer.
# Set EVIDENCE_MAX_IMAGE_KB to adjust; 0 disables.
max_kb="${EVIDENCE_MAX_IMAGE_KB:-700}"
if [ "$max_kb" != "0" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  max_bytes=$((max_kb * 1024))
  oversized=""
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    sz=$(wc -c < "$f" 2>/dev/null || echo 0)
    if [ "$sz" -gt "$max_bytes" ]; then
      oversized="${oversized}  $f ($((sz / 1024))KB > ${max_kb}KB)"$'\n'
    fi
  done < <(
    { git diff --cached --name-only --diff-filter=AM 2>/dev/null
      find evidence -maxdepth 3 -type f 2>/dev/null; } |
      grep -iE '\.(png|jpe?g|gif|webp|bmp)$' | sort -u
  )
  if [ -n "$oversized" ]; then
    echo "Blocked: image(s) exceed the ${max_kb}KB evidence size limit:"
    printf '%s' "$oversized"
    echo "Oversized images crash the reviewer (1MB message buffer < the file's base64),"
    echo "so the PR cannot be reviewed. Downscale before committing, e.g.:"
    echo "  ffmpeg -y -i img.png -vf scale=iw/2:-1 out.png && mv out.png img.png"
    echo "or re-capture at a lower resolution."
    exit 1
  fi
fi

# Fast gates only. Projects supply these via env (set in AGENT.md / repo CLAUDE.md).
# All target-owned commands run through prc_run_test_cmd (env scrub + #63
# re-entrancy bound); rc 213 = the command re-entered the gate — fail with the
# real cause, not a fake lint failure.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
[ -f "$HERE/_pr_common.sh" ] && . "$HERE/_pr_common.sh"
_run_fast_gate() {  # _run_fast_gate "<cmd>" "<Label>" "<VAR_NAME>"
  local rc=0
  if declare -f prc_run_test_cmd >/dev/null; then
    prc_run_test_cmd "$1"; rc=$?
  else
    eval "$1"; rc=$?
  fi
  if [ "$rc" -eq "${PRC_GATE_RECURSION_RC:-213}" ]; then
    echo "GATE RECURSION: $3 re-entered the harness gate and was refused (see #63) — fix the $3 configuration: $1"
    exit 1
  fi
  [ "$rc" -eq 0 ] || { echo "$2 failed ($3): $1"; exit 1; }
}
if [ -n "${LINT_CMD:-}" ]; then
  _run_fast_gate "$LINT_CMD" "Lint" "LINT_CMD"
fi
if [ -n "${TYPECHECK_CMD:-}" ]; then
  _run_fast_gate "$TYPECHECK_CMD" "Typecheck" "TYPECHECK_CMD"
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
      prc_run_test_cmd "$_tc" >/dev/null 2>&1; _trc=$?
      if [ "$_trc" -eq "${PRC_GATE_RECURSION_RC:-213}" ]; then
        echo "GATE RECURSION: the test command re-entered the harness gate and was"
        echo "refused (see #63) — cannot verify this red: commit is actually red."
        echo "Fix the test-command configuration (self-referential TEST_CMD/.harness/test-cmd): $_tc"
        exit 1
      fi
      if [ "$_trc" -eq 0 ]; then
        echo "'red:' commit but the test suite PASSES ($_tc) — the new test is not"
        echo "actually failing. Write the FAILING test first, before any implementation."
        exit 1
      fi
    fi
  fi
fi
exit 0
