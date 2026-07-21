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

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
base="${BASE_BRANCH:-dev}"

# Only governed-shape branches carry harness work; anything else → allow.
case "$branch" in
  feat/*|fix/*|chore/*|refactor/*) : ;;
  *) exit 0 ;;
esac

# Is there an OPEN PR for this branch? If not, this is a pre-PR push → allow
# (the create gate will enforce later). Fail OPEN on any gh/network error so a
# transient lookup failure never wedges the agent mid-work.
pr=$(gh pr list --head "$branch" --state open --json number --jq '.[0].number // ""' 2>/dev/null || echo "")
[ -n "$pr" ] || exit 0

# An open PR exists → re-verify the new state. Resolve the diff range vs base.
range=""
for ref in "origin/$base" "$base"; do
  if git rev-parse --verify --quiet "$ref" >/dev/null 2>&1; then
    range="$ref...HEAD"; break
  fi
done
# Can't resolve base → fail open (don't block a push over a local ref gap).
[ -n "$range" ] || exit 0

changed=$(git diff --name-only "$range" 2>/dev/null || echo "")

# Shared, state-derived checks (same code path as check-pr.sh).
prc_check_tests
prc_check_commit_log
prc_check_no_evidence "$changed"
prc_check_tests_immutable "$changed"

exit 0
