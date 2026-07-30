#!/usr/bin/env bash
# Governed: `git push` — but ONLY enforces when the current branch already has an
# OPEN PR. This re-runs the state-derived gate on address-review pushes (new
# commits added to a PR after creation), which the create-time gate never sees.
#
# Deliberately NOT enforced when there is no open PR yet: the first pushes while
# building a branch are normal; check-pr.sh gates the PR at creation time.
#
# Shares its checks with check-pr.sh via _pr_common.sh (tests, commit hygiene,
# evidence, test-immutability). Body/marker/adversarial checks are create-only.
set -uo pipefail
fail() { echo "Push blocked: $1"; exit 1; }
# shellcheck source=_pr_common.sh
. "$(dirname "$0")/_pr_common.sh"

base="${BASE_BRANCH:-dev}"

prc_check_push_destination() {
  local branch="$1"
  local pr="" pr_rc=1 baseref diff_range log_range changed

  # Only governed-shape branches carry harness work; anything else is not this
  # gate's business, but later destinations in the same push still are.
  case "$branch" in
    feat/*|fix/*|chore/*|refactor/*) : ;;
    *) return 0 ;;
  esac

  # Is there an OPEN PR for this destination? If not, this is a pre-PR push.
  # Lookup failures retry once and then fail closed.
  for _attempt in 1 2; do
    pr=$(gh pr list --head "$branch" --state open --json number --jq '.[0].number // ""' 2>/dev/null)
    pr_rc=$?
    [ "$pr_rc" -eq 0 ] && break
    [ "$_attempt" = 1 ] && sleep "${GATE_GH_RETRY_SLEEP:-2}"
  done
  [ "$pr_rc" -eq 0 ] \
    || fail "could not verify PR state for branch '$branch' — 'gh pr list' failed twice (gh/network/auth error). The push gate needs to know whether an open PR exists, so it fails CLOSED rather than letting an unverified push through. Check connectivity and 'gh auth status', then retry the push shortly."
  [ -n "$pr" ] || return 0

  # Resolve the base once per governed destination. The working-tree state is
  # shared, while the issue identity and open-PR lookup belong to the ref being
  # written.
  baseref="$(prc_resolve_base_ref)"
  [ -n "$baseref" ] || return 0
  diff_range="$baseref...HEAD"
  log_range="$baseref..HEAD"
  changed=$(git diff --name-only "$diff_range" 2>/dev/null || echo "")

  prc_check_commit_log
  prc_check_no_evidence "$changed"
  prc_check_no_generated_instructions "$changed"
  prc_check_tests_immutable "$changed"
  prc_check_tests
}

prc_normalize_push_destination() {
  local destination="${1#+}"
  case "$destination" in *:*) destination="${destination#*:}" ;; esac
  destination="${destination#refs/heads/}"
  case "$destination" in
    HEAD|@) printf '%s' "$checked_out_branch" ;;
    *) printf '%s' "$destination" ;;
  esac
}

checked_out_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
destinations=()
remote_seen=""
for arg in "$@"; do
  [ "$arg" = "push" ] && continue
  case "$arg" in -*) continue ;; esac
  if [ -z "$remote_seen" ]; then
    remote_seen=1
    case "$arg" in
      +*|*:*) destinations+=("$(prc_normalize_push_destination "$arg")") ;;
    esac
    continue
  fi
  destinations+=("$(prc_normalize_push_destination "$arg")")
done

if [ "${#destinations[@]}" -eq 0 ]; then
  destinations=("$checked_out_branch")
fi
for branch in "${destinations[@]}"; do
  prc_check_push_destination "$branch"
done

exit 0
