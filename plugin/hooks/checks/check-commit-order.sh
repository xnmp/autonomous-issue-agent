#!/usr/bin/env bash
# Governed (NEW hook): runs on every `git commit` IN ADDITION to check-commit.sh.
# Receives the commit-onward tokens as argv.
#
# Enforces the TDD ordering invariant the base specs describe but never gate:
#
#   A `green:` commit (implementation) is only allowed if a `red:` commit
#   (failing tests) already exists on this branch since it diverged from the
#   base branch. You cannot implement before you have a failing test.
#
# This makes red-before-green mechanical, not honour-system. The commit being
# validated has NOT happened yet, so we inspect the message in argv against the
# log of commits already on the branch.
#
# Allow paths (fail-open, by design — the hook is a fast nudge, CI/review/PR-gate
# are the real boundary):
#   * non-green prefixes (plan/red/docs/chore/refactor/test/fix/feat) — ordering
#     only constrains green;
#   * `--amend` (re-editing an existing commit; ordering already established);
#   * file/heredoc messages we can't read here (-F) — PR gate re-derives truth;
#   * the base branch ref cannot be resolved (e.g. not fetched) — we note it on
#     stderr and allow, rather than wedge a legitimate commit.
set -uo pipefail

BASE_BRANCH="${BASE_BRANCH:-dev}"

# Read the incoming commit message prefix + whether this is an --amend.
msg=""
amend=0
for ((i = 1; i <= $#; i++)); do
  case "${!i}" in
    --amend)        amend=1 ;;
    -m | --message) j=$((i + 1)); msg="${!j:-}" ;;
    -m*)            msg="${!i#-m}" ;;
    --message=*)    msg="${!i#--message=}" ;;
  esac
done

# Phase detection (bare `green:` OR conventional-scoped `feat(green):`). Shared
# helper from _pr_common.sh so the two forms are treated identically. See there.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
[ -f "$HERE/_pr_common.sh" ] && . "$HERE/_pr_common.sh"
declare -f commit_phase >/dev/null || commit_phase() { case "$1" in green:*) echo green;; *\(green\):*) echo green;; *) echo "";; esac; }

# Only green: commits are ordering-constrained. A non-green --amend (re-editing a
# tip) is safe and allowed by the case below. But `git commit --amend -m 'green:'`
# was a real bypass — commit a benign prefix, then amend it to green: with no red:
# on the branch — so an amend that RESULTS in green: is STILL ordering-checked.
if [ "$(commit_phase "$msg")" != "green" ]; then
  exit 0                          # not a green commit (or unreadable) → allow
fi
: "$amend"  # referenced for clarity; green: is constrained whether or not amend

# Resolve a base..HEAD range to list commits already on this branch.
# TWO dots, deliberately: three dots is `git log`'s SYMMETRIC difference, which
# would also list the base branch's own commits. This checker was always right;
# _pr_common.sh's shared range was not (review M6) — do not "unify" them into one
# variable, they are different ranges. See _pr_common.sh's header.
range=""
for ref in "origin/$BASE_BRANCH" "$BASE_BRANCH"; do
  if git rev-parse --verify --quiet "$ref" >/dev/null 2>&1; then
    range="$ref..HEAD"
    break
  fi
done

if [ -z "$range" ]; then
  echo "check-commit-order: base ref '$BASE_BRANCH' not resolvable; allowing (gate degraded)." >&2
  exit 0
fi

# Is there already a red: commit on this branch? (bare `red: ` OR scoped `…(red): `)
if git log "$range" --format='%s' 2>/dev/null | grep -qE '^(red: |[a-z]+\(red\): )'; then
  exit 0
fi

echo "TDD ordering violation: a 'green:' commit requires a prior 'red:' commit on this branch."
echo "Commit your failing tests first:  git commit -m 'red: <failing test for the behaviour>'"
echo "(branch commits since $BASE_BRANCH:)"
git log "$range" --format='  %s' 2>/dev/null | head -20
exit 1
