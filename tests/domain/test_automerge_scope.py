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

GREEN = (Check(name="ci", state="SUCCESS"),)


def _cfg(**kw):
    values = {
        "auto_merge": True,
        "identity_mode": IdentityMode.THREE,
        "arm_on_open": True,
        "fix_bot": "fix-bot",
    }
    values.update(kw)
    return FixConfig(**values)


def _pr(branch: str, **kw):
    head = str(kw.pop("head_ref_oid", "head-532"))
    kw.setdefault("merge_state", "CLEAN")
    kw.setdefault(
        "reviews",
        (
            Review(
                author="review-bot",
                state="APPROVED",
                commit_id=head,
            ),
        ),
    )
    kw.setdefault("checks", GREEN)
    return PRView(
        number=532,
        head_ref_name=branch,
        head_ref_oid=head,
        **kw,
    )


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
                head_ref_oid="head", reviews=(
                    Review("APPROVED", "review-bot", commit_id="head"),
                ), checks=GREEN,
                mergeable="MERGEABLE", merge_state="BEHIND")
    assert should_update_branch(pr, FixState(), _cfg()) is False


def test_harness_prs_are_still_branch_updated():
    pr = PRView(number=532, head_ref_name="fix/504-tab-titles",
                head_ref_oid="head", reviews=(
                    Review("APPROVED", "review-bot", commit_id="head"),
                ), checks=GREEN,
                mergeable="MERGEABLE", merge_state="BEHIND")
    assert should_update_branch(pr, FixState(), _cfg()) is True


# ── arm_on_open: protection-verified early arming ─────────────────────────────
PENDING = (Check(name="ci", state="IN_PROGRESS", bucket="pending"),)


def test_verified_gate_never_bypasses_current_head_review_and_ci():
    pr = PRView(
        number=540,
        head_ref_name="fix/522-lanes",
        head_ref_oid="head",
        merge_state="CLEAN",
        reviews=(),
        checks=PENDING,
    )
    assert should_automerge(pr, FixState(), _cfg()) is False


def test_arm_on_open_still_respects_scope_and_conflicts():
    """Early arming relaxes ONLY the review/CI wait; every ownership and
    once-only guard keeps its teeth."""
    cfg = _cfg(arm_on_open=True)
    foreign = PRView(number=1, head_ref_name="dependabot/npm/x", reviews=(), checks=())
    assert should_automerge(foreign, FixState(), cfg) is False
    ours = PRView(number=2, head_ref_name="fix/9-t", reviews=(), checks=())
    assert should_automerge(ours, FixState(), cfg) is False
    conflicted = PRView(number=3, head_ref_name="fix/9-t", reviews=(), checks=(),
                        mergeable="CONFLICTING")
    assert should_automerge(conflicted, FixState(), cfg) is False
    solo = FixConfig(auto_merge=True, identity_mode=IdentityMode.SOLO,
                     arm_on_open=True,
                     fix_bot="fix-bot")
    assert should_automerge(ours, FixState(), solo) is False


def test_unreviewed_or_red_ci_never_merges_regardless_of_server_gate():
    unreviewed = PRView(number=4, head_ref_name="fix/9-t", reviews=(), checks=GREEN)
    assert should_automerge(unreviewed, FixState(), _cfg(arm_on_open=False)) is False
    red = _pr("fix/9-t",
                 checks=(Check(name="ci", state="COMPLETED", bucket="fail"),))
    assert should_automerge(red, FixState(), _cfg(arm_on_open=False)) is False


def test_exact_head_approval_and_green_ci_merge_without_server_gate():
    """An unprotected base must not disable merging outright: the exact-head
    distinct-actor approval + exact-head green CI below ARE the gate there
    (2026-08-01 voyage: requiring the server gate unconditionally zeroed
    merges on every repo whose base cannot carry verifiable protection)."""
    pr = _pr("fix/9-t")
    assert should_automerge(pr, FixState(), _cfg(arm_on_open=False)) is True
    # …and the approval must still bind the CURRENT head, server gate or not.
    stale = _pr(
        "fix/9-t",
        head_ref_oid="new-head",
        reviews=(Review(author="review-bot", state="APPROVED", commit_id="old-head"),),
    )
    assert should_automerge(stale, FixState(), _cfg(arm_on_open=False)) is False


# ── #48: a failed checks read must never arm a merge ──────────────────────────
def test_unread_checks_do_not_automerge():
    """The hole: a transient `gh pr checks` failure yielded checks=() → NONE →
    treated like OK, and an approved PR could arm auto-merge on a tick whose
    checks were never read. UNKNOWN closes the gate; next successful read
    re-opens it (cost: one poll of latency)."""
    pr = PRView(number=532, head_ref_name="fix/504-tab-titles",
                head_ref_oid="head", merge_state="CLEAN",
                reviews=(Review("APPROVED", "review-bot", commit_id="head"),),
                checks=(), checks_known=False)
    assert should_automerge(pr, FixState(), _cfg()) is False


def test_honest_no_checks_still_automerges():
    """The no-CI-repo path is deliberate and must not regress: a SUCCESSFUL
    read of zero checks (honest NONE) stays mergeable."""
    pr = _pr("fix/504-tab-titles", checks=())
    assert should_automerge(pr, FixState(), _cfg()) is True


def test_unread_checks_do_not_branch_update():
    pr = PRView(number=532, head_ref_name="fix/504-tab-titles",
                head_ref_oid="head",
                reviews=(Review("APPROVED", "review-bot", commit_id="head"),),
                checks=(), checks_known=False,
                mergeable="MERGEABLE", merge_state="BEHIND")
    assert should_update_branch(pr, FixState(), _cfg()) is False


def test_verified_gate_does_not_turn_unread_checks_into_merge_evidence():
    pr = PRView(number=540, head_ref_name="fix/522-lanes", reviews=(),
                checks=(), checks_known=False)
    assert should_automerge(pr, FixState(), _cfg()) is False
