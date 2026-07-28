"""Auto-merge is confined to branches the harness authored.

The reviewer adopts unlabelled PRs by default (REVIEW_ADOPT_UNLABELED) so a PR
that bypassed the pipeline still gets reviewed. Composed with AUTO_MERGE=1 that
meant "approve and merge ANY open PR into the base branch" — and on the first
real deployment the reviewer adopted dependabot's #486 on a repo with five
dependency PRs queued. Reviewing a stranger's PR is the feature; merging it is
not.
"""
from __future__ import annotations

import pytest

from issue_agent.domain.fix import FixConfig, FixState, should_automerge, should_update_branch
from issue_agent.domain.world import Check, PRView, Review
from issue_agent.domain.review import IdentityMode

APPROVED = (Review(author="review-bot", state="APPROVED"),)
GREEN = (Check(name="ci", state="SUCCESS"),)


def _cfg(**kw):
    return FixConfig(auto_merge=True, identity_mode=IdentityMode.THREE,
                     fix_bot="fix-bot", **kw)


def _pr(branch: str, **kw):
    return PRView(number=532, head_ref_name=branch, reviews=APPROVED,
                  checks=GREEN, **kw)


@pytest.mark.parametrize("branch", [
    "fix/504-tab-titles", "feat/12-thing", "chore/9-bump", "refactor/3-seam",
])
def test_a_harness_authored_pr_still_automerges(branch):
    assert should_automerge(_pr(branch), FixState(), _cfg()) is True


@pytest.mark.parametrize("branch", [
    "dependabot/cargo/src-tauri/lru-0.18.1",
    "dependabot/npm_and_yarn/typescript-6.0.3",
    "renovate/some-dep",
    "my-hand-made-branch",
])
def test_an_adopted_foreign_pr_is_never_automerged(branch):
    """The regression: approved + green + three-identity was enough to merge."""
    assert should_automerge(_pr(branch), FixState(), _cfg()) is False


def test_a_conflicting_pr_is_not_armed():
    """A CONFLICTING PR cannot merge no matter what gh is told — arming it
    burned a gh call + verification probe and logged a refusal EVERY poll
    until the rebase landed (#534, 2026-07-26). The rebase leg owns it;
    automerge re-fires the first poll after the branch is mergeable again."""
    pr = _pr("fix/506-icon", mergeable="CONFLICTING")
    assert should_automerge(pr, FixState(), _cfg()) is False
    assert should_automerge(_pr("fix/506-icon", mergeable="MERGEABLE"),
                            FixState(), _cfg()) is True


def test_a_closing_reference_cannot_buy_a_foreign_pr_a_merge():
    """`Fixes #504` in a stranger's body populates closing_issue, so keying the
    check on the linked issue would let anyone opt their PR into auto-merge.
    The branch name is the part only the fixer creates."""
    pr = _pr("dependabot/cargo/lru-0.18.1", closing_issue=504)
    assert should_automerge(pr, FixState(), _cfg()) is False


def test_foreign_prs_are_not_branch_updated_either():
    """should_update_branch PUSHES to the PR's branch."""
    pr = PRView(number=532, head_ref_name="dependabot/cargo/lru-0.18.1",
                reviews=APPROVED, checks=GREEN,
                mergeable="MERGEABLE", merge_state="BEHIND")
    assert should_update_branch(pr, _cfg()) is False


def test_harness_prs_are_still_branch_updated():
    pr = PRView(number=532, head_ref_name="fix/504-tab-titles",
                reviews=APPROVED, checks=GREEN,
                mergeable="MERGEABLE", merge_state="BEHIND")
    assert should_update_branch(pr, _cfg()) is True


# ── arm_on_open: protection-verified early arming ─────────────────────────────
PENDING = (Check(name="ci", state="IN_PROGRESS", bucket="pending"),)


def test_arm_on_open_arms_before_review_and_ci():
    """With a VERIFIED review+checks gate on the base branch, waiting for
    APPROVE+green here only adds daemon latency (#539 sat approved+green for
    the length of an unrelated resolve, 2026-07-27) — GitHub holds the gate."""
    pr = PRView(number=540, head_ref_name="fix/522-lanes", reviews=(), checks=PENDING)
    assert should_automerge(pr, FixState(), _cfg(arm_on_open=True)) is True


def test_arm_on_open_still_respects_scope_marker_and_conflicts():
    """Early arming relaxes ONLY the review/CI wait; every ownership and
    once-only guard keeps its teeth."""
    cfg = _cfg(arm_on_open=True)
    foreign = PRView(number=1, head_ref_name="dependabot/npm/x", reviews=(), checks=())
    assert should_automerge(foreign, FixState(), cfg) is False
    ours = PRView(number=2, head_ref_name="fix/9-t", reviews=(), checks=())
    assert should_automerge(ours, FixState(automerge_enabled=frozenset({2})), cfg) is False
    conflicted = PRView(number=3, head_ref_name="fix/9-t", reviews=(), checks=(),
                        mergeable="CONFLICTING")
    assert should_automerge(conflicted, FixState(), cfg) is False
    solo = FixConfig(auto_merge=True, identity_mode=IdentityMode.SOLO,
                     fix_bot="fix-bot", arm_on_open=True)
    assert should_automerge(ours, FixState(), solo) is False


def test_without_arm_on_open_unreviewed_or_red_prs_stay_unarmed():
    """The default path is unchanged: no verified server-side gate means the
    harness itself is the gate, and it waits for APPROVE + green."""
    unreviewed = PRView(number=4, head_ref_name="fix/9-t", reviews=(), checks=GREEN)
    assert should_automerge(unreviewed, FixState(), _cfg()) is False
    red = PRView(number=5, head_ref_name="fix/9-t", reviews=APPROVED,
                 checks=(Check(name="ci", state="COMPLETED", bucket="fail"),))
    assert should_automerge(red, FixState(), _cfg()) is False


# ── #48: a failed checks read must never arm a merge ──────────────────────────
def test_unread_checks_do_not_automerge():
    """The hole: a transient `gh pr checks` failure yielded checks=() → NONE →
    treated like OK, and an approved PR could arm auto-merge on a tick whose
    checks were never read. UNKNOWN closes the gate; next successful read
    re-opens it (cost: one poll of latency)."""
    pr = PRView(number=532, head_ref_name="fix/504-tab-titles",
                reviews=APPROVED, checks=(), checks_known=False)
    assert should_automerge(pr, FixState(), _cfg()) is False


def test_honest_no_checks_still_automerges():
    """The no-CI-repo path is deliberate and must not regress: a SUCCESSFUL
    read of zero checks (honest NONE) stays mergeable."""
    pr = PRView(number=532, head_ref_name="fix/504-tab-titles",
                reviews=APPROVED, checks=())
    assert should_automerge(pr, FixState(), _cfg()) is True


def test_unread_checks_do_not_branch_update():
    pr = PRView(number=532, head_ref_name="fix/504-tab-titles",
                reviews=APPROVED, checks=(), checks_known=False,
                mergeable="MERGEABLE", merge_state="BEHIND")
    assert should_update_branch(pr, _cfg()) is False


def test_arm_on_open_is_indifferent_to_unread_checks():
    """Early arming rests on GitHub's verified server-side gate, which reads
    the checks itself — the harness's failed read changes nothing."""
    pr = PRView(number=540, head_ref_name="fix/522-lanes", reviews=(),
                checks=(), checks_known=False)
    assert should_automerge(pr, FixState(), _cfg(arm_on_open=True)) is True
