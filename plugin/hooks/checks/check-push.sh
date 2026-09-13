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
. "$(dirname "$0")/_branch_policy.sh"

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
base="${BASE_BRANCH:-dev}"

# Only governed-shape branches carry harness work; anything else → allow.
[[ "$branch" =~ $HARNESS_BRANCH_PREFIX_RE ]] || exit 0

# Is there an OPEN PR for this branch? If not, this is a pre-PR push → allow
# (the create gate will enforce later).
#
# FAIL CLOSED when the lookup itself fails. This used to `|| echo ""` and treat
# a gh/network error as "no PR" — i.e. the gate's own precondition was a bypass:
# one flaky `gh` call (proxy hiccup, expired token, rate limit) silently skipped
# the ENTIRE push gate on a branch that does have an open PR. Retry once first,
# because a transient blip is the common case and a needless denial costs the
# agent a turn; if it still fails we deny, since a blocked push is recoverable
# (the agent just pushes again) but an ungated push is not.
# GATE_GH_RETRY_SLEEP is a knob only so the self-test can drive this path fast.
pr=""; pr_rc=1
for _attempt in 1 2; do
  pr=$(gh pr list --head "$branch" --state open --json number --jq '.[0].number // ""' 2>/dev/null)
  pr_rc=$?
  [ "$pr_rc" -eq 0 ] && break
  [ "$_attempt" = 1 ] && sleep "${GATE_GH_RETRY_SLEEP:-2}"
done
[ "$pr_rc" -eq 0 ] \
  || fail "could not verify PR state for branch '$branch' — 'gh pr list' failed twice (gh/network/auth error). The push gate needs to know whether an open PR exists, so it fails CLOSED rather than letting an unverified push through. Check connectivity and 'gh auth status', then retry the push shortly."
[ -n "$pr" ] || exit 0

# An open PR exists → re-verify the new state. Resolve the base ref, then the two
# distinct ranges (review M6): three dots for `git diff`, TWO for `git log`. One
# shared three-dot range made prc_check_commit_log read the base branch's own
# commits, so every push to an open PR was blocked as soon as anything merged
# into the base ("Merge pull request #7 …" carries no allowed prefix) — on the
# ADDRESS_REVIEW / FIX_CI legs, i.e. exactly when the base HAS advanced.
baseref="$(prc_resolve_base_ref)"
# Can't resolve base → fail open (don't block a push over a local ref gap).
[ -n "$baseref" ] || exit 0
diff_range="$baseref...HEAD"
log_range="$baseref..HEAD"

changed=$(git diff --name-only "$diff_range" 2>/dev/null || echo "")

# Shared, state-derived checks (same code path as check-pr.sh). Cheap structural
# checks first, the suite-running one last — same order as
# adapters/governor.py:_push_gate_for_branch (review M21).
prc_check_commit_log
prc_check_no_evidence "$changed"
prc_check_no_harness_state "$changed"
prc_check_no_generated_instructions "$changed"
prc_check_tests_immutable "$changed"
prc_check_tests

exit 0
