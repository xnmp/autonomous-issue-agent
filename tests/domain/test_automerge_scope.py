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
