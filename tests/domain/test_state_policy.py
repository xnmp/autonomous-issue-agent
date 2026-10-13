"""The derive/policy core against the minimal-harness tick table and the
refactor decisions (Q1 identity, Q2 rounds, Q3 attempts/resets, Q7 caps,
Q8 replies, Q10 merge, Q16 machine backoff)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.actions import (
    AgentKind,
    MergePullRequest,
    OpenPullRequest,
    PostComment,
    Relabel,
    RunAgent,
    Target,
    UpdateBranch,
)
from issue_agent.domain.backoff import LegEnd
from issue_agent.domain.checks import Check, Observed, RequiredCheck, Unavailable
from issue_agent.domain.labels import Label
from issue_agent.domain.policy import QUESTION_LIMIT, actions, question_text
from issue_agent.domain.refs import harness_branch_issue
from issue_agent.domain.state import (
    Backoff,
    Busy,
    Dispatch,
    Harness,
    ItemKey,
    Local,
    Merge,
    NeedsHuman,
    OpenPR,
    Refresh,
    Release,
    StopReason,
    Wait,
    WaitReason,
    derive,
    human_comments,
    issue_key,
    pr_key,
    stop_tag,
)
from issue_agent.domain.world import Branch, Comment, IssueView, PRView, Push, Review, WorldState

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=1)
H = Harness("fix-bot", "review-bot")
HEAD = "h1"
FIX, REVIEW, ADDR = AgentKind.FIX_CI, AgentKind.REVIEW, AgentKind.ADDRESS_REVIEW


def at(minute: float) -> datetime:
    return T0 + timedelta(minutes=minute)


def iso(minute: float) -> str:
    return at(minute).isoformat()


def ci(bucket: str = "pass", head: str = HEAD, state: str = "COMPLETED") -> Observed:
    check = Check("test", state=state, bucket=bucket, required=True)
    return Observed(head, head, frozenset({RequiredCheck("test")}), (check,))


GREEN, RED, PENDING = ci("pass"), ci("fail"), ci("pending", state="IN_PROGRESS")


def pr(number: int = 7, issue: int = 3, **kw) -> PRView:
    kw.setdefault("head_ref_name", f"fix/{issue}-slug")
    kw.setdefault("head_ref_oid", HEAD)
    kw.setdefault("ci", GREEN)
    kw.setdefault("mergeable", "MERGEABLE")
    kw.setdefault("merge_state", "CLEAN")
    kw["labels"] = frozenset(kw.get("labels", ()))
    return PRView(number=number, **kw)


def issue(number: int = 3, *labels: Label, **kw) -> IssueView:
    return IssueView(number=number, labels=frozenset((Label.AI_READY, *labels)), **kw)


def review(state: str, minute: float, author: str = "review-bot", head: str = HEAD) -> Review:
    return Review(state=state, author=author, submitted_at=iso(minute), commit_id=head)


def human(minute: float, author: str = "alice", body: str = "please continue") -> Comment:
    return Comment(author, body, iso(minute))


def stop(reason: StopReason, minute: float) -> Comment:
    return Comment("fix-bot", f"{H.marker}\n{stop_tag(reason)}\nstopped", iso(minute))


def legs(kind: AgentKind, *minutes: float, machine: bool = False) -> tuple[LegEnd, ...]:
    return tuple(LegEnd(kind, at(m), machine) for m in minutes)


def local(legs_by: dict | None = None, **kw) -> Local:
    return Local(now=kw.pop("now", NOW), legs=legs_by or {}, **kw)


def one(world: WorldState, loc: Local | None = None):
    states = derive(world, loc or local(), H)
    assert len(states) == 1, states
    return next(iter(states.values()))


def pr_state(p: PRView, loc: Local | None = None):
    return one(WorldState(prs=(p,)), loc)


def pr_legs(*ends: LegEnd, number: int = 7) -> dict:
    return {pr_key(number): tuple(ends)}


# ── Item identity (Q1) ───────────────────────────────────────────────────────


def test_ready_issue_without_a_harness_pr_dispatches_resolve():
    world = WorldState(issues=(issue(3),))
    assert derive(world, local(), H) == {issue_key(3): Dispatch(AgentKind.RESOLVE)}
    assert actions(derive(world, local(), H), H) == (RunAgent(AgentKind.RESOLVE, 3, head=""),)


@pytest.mark.parametrize(
    "view",
    [
        IssueView(3),  # not admitted
        IssueView(3, labels=frozenset({Label.AI_READY}), state="closed"),
    ],
)
def test_unadmitted_or_closed_issues_are_not_items(view):
    assert derive(WorldState(issues=(view,)), local(), H) == {}


def test_an_open_harness_pr_replaces_its_issue_as_the_item():
    states = derive(WorldState(issues=(issue(3),), prs=(pr(7, 3),)), local(), H)
    assert set(states) == {pr_key(7)}


@pytest.mark.parametrize("branch", ["feat/3-x", "fix/3", "fix/0-x", "fix/03-x", "xfix/3-x", "main", "", "fix/-3-x"])
def test_only_fix_issue_branches_are_harness_prs(branch):
    world = WorldState(issues=(issue(3),), prs=(pr(7, head_ref_name=branch),))
    assert set(derive(world, local(), H)) == {issue_key(3)}


def test_closed_harness_pr_does_not_shadow_the_issue():
    world = WorldState(issues=(issue(3),), prs=(pr(7, 3, state="CLOSED"),))
    assert derive(world, local(), H) == {issue_key(3): Dispatch(AgentKind.RESOLVE)}


@pytest.mark.parametrize("raw", [None, 3, "fix/12345678901-x"])
def test_harness_branch_parser_rejects_malformed_input(raw):
    assert harness_branch_issue(raw) is None  # type: ignore[arg-type]


# ── PR rows of the tick table ────────────────────────────────────────────────


def test_red_required_check_on_head_dispatches_fix_ci():
    assert pr_state(pr(ci=RED)) == Dispatch(FIX, HEAD)


def test_conflicted_pr_rebases_even_when_red_or_unread():
    assert pr_state(pr(mergeable="CONFLICTING", ci=RED)) == Dispatch(AgentKind.REBASE, HEAD)
    assert pr_state(pr(mergeable="CONFLICTING", ci=Unavailable("x"))) == Dispatch(AgentKind.REBASE, HEAD)


def test_behind_but_clean_green_pr_updates_its_branch_for_free():
    states = derive(WorldState(prs=(pr(merge_state="BEHIND"),)), local(headroom=False), H)
    assert states == {pr_key(7): Refresh(HEAD)}
    assert actions(states, H) == (UpdateBranch(7, HEAD),)


@pytest.mark.parametrize(
    "reviews",
    [
        (),
        (review("APPROVED", 1, head="old"),),
        (review("COMMENTED", 1),),
        (review("APPROVED", 1, author="alice"),),
        (review("DISMISSED", 1),),
    ],
)
def test_green_pr_without_a_bot_verdict_on_head_is_reviewed(reviews):
    assert pr_state(pr(reviews=reviews)) == Dispatch(REVIEW, HEAD)


def test_bot_changes_requested_on_head_dispatches_address_review():
    assert pr_state(pr(reviews=(review("CHANGES_REQUESTED", 1),))) == Dispatch(ADDR, HEAD)


def test_bot_approval_on_green_head_merges_bound_to_that_head():
    states = derive(WorldState(prs=(pr(reviews=(review("APPROVED", 1),)),)), local(headroom=False), H)
    assert states == {pr_key(7): Merge(HEAD)}
    assert actions(states, H) == (MergePullRequest(7, HEAD),)


def test_approval_never_merges_a_red_head():
    assert pr_state(pr(ci=RED, reviews=(review("APPROVED", 1),))) == Dispatch(FIX, HEAD)


def test_latest_bot_verdict_on_head_wins():
    assert pr_state(pr(reviews=(review("CHANGES_REQUESTED", 1), review("APPROVED", 2)))) == Merge(HEAD)
    assert pr_state(pr(reviews=(review("APPROVED", 1), review("CHANGES_REQUESTED", 2)))) == Dispatch(ADDR, HEAD)


def test_push_after_approval_needs_a_fresh_review():
    assert pr_state(pr(head_ref_oid="h2", ci=ci(head="h2"), reviews=(review("APPROVED", 1),))) == Dispatch(REVIEW, "h2")


@pytest.mark.parametrize(
    ("view", "reason"),
    [
        (pr(ci=PENDING), WaitReason.CI_PENDING),
        (pr(ci=Unavailable("read failed")), WaitReason.CI_UNKNOWN),
        (pr(ci=ci(head="stale")), WaitReason.CI_UNKNOWN),
        (pr(head_ref_oid=""), WaitReason.HEAD_UNKNOWN),
        (pr(mergeable="UNKNOWN", reviews=(review("APPROVED", 1),)), WaitReason.MERGEABILITY_UNKNOWN),
    ],
)
def test_unsettled_pr_waits(view, reason):
    assert pr_state(view) == Wait(reason)


# ── Workflow paths ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "./.github/CODEOWNERS", ".github/actions/a/x.yml"])
def test_pr_touching_github_paths_needs_a_human(path):
    states = derive(WorldState(prs=(pr(changed_files=frozenset({"src/a.py", path})),)), local(), H)
    assert states == {pr_key(7): NeedsHuman(StopReason.WORKFLOW_PATH, "-")}
    label, comment = actions(states, H)
    assert label == Relabel(Target.PR, 7, remove=None, add=Label.NEEDS_HUMAN)
    assert isinstance(comment, PostComment) and H.marker in comment.body and comment.key


@pytest.mark.parametrize("path", ["src/.github/x", ".githubx/y", "docs/github.md"])
def test_non_github_paths_do_not_stop(path):
    assert pr_state(pr(changed_files=frozenset({path}))) == Dispatch(REVIEW, HEAD)


def test_released_workflow_stop_lets_the_pr_proceed():
    view = pr(changed_files=frozenset({".github/workflows/ci.yml"}), comments=(stop(StopReason.WORKFLOW_PATH, 1),))
    assert pr_state(view) == Dispatch(REVIEW, HEAD)


# ── needs-human and replies (Q8) ─────────────────────────────────────────────


def labelled(comments=(), **kw) -> PRView:
    return pr(labels={Label.NEEDS_HUMAN}, comments=tuple(comments), **kw)


def test_needs_human_waits_without_a_reply():
    assert pr_state(labelled([stop(StopReason.ATTEMPTS, 10)])) == Wait(WaitReason.NEEDS_HUMAN)


def test_reply_newer_than_stop_and_last_leg_releases_and_policy_removes_the_label():
    view = labelled([stop(StopReason.ATTEMPTS, 10), human(20)])
    states = derive(WorldState(prs=(view,)), local(pr_legs(*legs(FIX, 5))), H)
    assert states == {pr_key(7): Release()}
    assert actions(states, H) == (Relabel(Target.PR, 7, remove=Label.NEEDS_HUMAN, add=None),)


@pytest.mark.parametrize(
    "comments",
    [
        [human(5), stop(StopReason.ATTEMPTS, 10)],  # older than the stop
        [stop(StopReason.ATTEMPTS, 10), Comment("fix-bot", f"{H.marker} note", iso(20))],  # harness-marked
        [stop(StopReason.ATTEMPTS, 10), Comment("codecov[bot]", "report", iso(20))],  # a bot
        [stop(StopReason.ATTEMPTS, 10), Comment("alice", "go", "yesterday")],  # unparseable time
        [human(20)],  # no harness stop comment
    ],
)
def test_non_qualifying_comments_do_not_release(comments):
    assert pr_state(labelled(comments)) == Wait(WaitReason.NEEDS_HUMAN)


def test_a_reply_after_the_label_was_applied_releases():
    view = labelled([stop(StopReason.ATTEMPTS, 10), human(20)], needs_human_labeled_at=iso(10))
    assert pr_state(view) == Release()


def test_an_operator_park_after_the_reply_is_not_undone():
    # Stop at 10, reply at 20 (released), operator re-applies the label at 30
    # before any leg ran: the old reply must not release the new park.
    view = labelled([stop(StopReason.ATTEMPTS, 10), human(20)], needs_human_labeled_at=iso(30))
    assert pr_state(view) == Wait(WaitReason.NEEDS_HUMAN)
    replied = labelled([stop(StopReason.ATTEMPTS, 10), human(20), human(40)], needs_human_labeled_at=iso(30))
    assert pr_state(replied) == Release()
    parked = IssueView(
        3,
        labels=frozenset({Label.AI_READY, Label.NEEDS_HUMAN}),
        comments=(stop(StopReason.ATTEMPTS, 10), human(20)),
        needs_human_labeled_at=iso(30),
    )
    assert one(WorldState(issues=(parked,))) == Wait(WaitReason.NEEDS_HUMAN)


def test_an_unparseable_label_time_never_releases():
    view = labelled([stop(StopReason.ATTEMPTS, 10), human(20)], needs_human_labeled_at="yesterday")
    assert pr_state(view) == Wait(WaitReason.NEEDS_HUMAN)


def test_reply_must_postdate_the_last_leg_end():
    view = labelled([stop(StopReason.ATTEMPTS, 10), human(20)])
    assert pr_state(view, local(pr_legs(*legs(REVIEW, 30)))) == Wait(WaitReason.NEEDS_HUMAN)


def test_reply_from_the_fix_bot_login_counts_when_unmarked():
    # FIX_BOT can be the operator's own login (Q1), so authorship is by marker.
    assert pr_state(labelled([stop(StopReason.ATTEMPTS, 10), human(20, author="fix-bot")])) == Release()


def test_released_item_proceeds_with_attempts_reset_past_the_stop():
    # Label removed (by policy after a reply, or by a human): the stop comment
    # is the release boundary, so the three earlier attempts no longer count.
    view = pr(ci=RED, comments=(stop(StopReason.ATTEMPTS, 10),))
    assert pr_state(view, local(pr_legs(*legs(FIX, 1, 2, 3)))) == Dispatch(FIX, HEAD)


def test_issue_stage_needs_human_uses_the_issue_thread():
    view = issue(3, Label.NEEDS_HUMAN, comments=(stop(StopReason.ATTEMPTS, 10), human(20)))
    states = derive(WorldState(issues=(view,)), local(), H)
    assert actions(states, H) == (Relabel(Target.ISSUE, 3, remove=Label.NEEDS_HUMAN, add=None),)


# ── Attempts (Q3) ────────────────────────────────────────────────────────────


def test_two_attempts_dispatch_and_the_third_stops_with_a_reason():
    view = pr(ci=RED)
    assert pr_state(view, local(pr_legs(*legs(FIX, 1, 2)))) == Dispatch(FIX, HEAD)
    states = derive(WorldState(prs=(view,)), local(pr_legs(*legs(FIX, 1, 2, 3))), H)
    assert states == {pr_key(7): NeedsHuman(StopReason.ATTEMPTS, iso(3), FIX)}
    label, comment = actions(states, H)
    assert label.add is Label.NEEDS_HUMAN
    assert "FIX_CI" in comment.body and stop_tag(StopReason.ATTEMPTS) in comment.body


def test_resolve_attempts_cap_on_the_issue():
    states = derive(WorldState(issues=(issue(3),)), local({issue_key(3): legs(AgentKind.RESOLVE, 1, 2, 3)}), H)
    assert states == {issue_key(3): NeedsHuman(StopReason.ATTEMPTS, iso(3), AgentKind.RESOLVE)}


def test_machine_failures_never_count_as_attempts():
    history = legs(FIX, 1, 2, 3, 4, 5, machine=True) + legs(FIX, 6, 7)
    assert pr_state(pr(ci=RED), local(pr_legs(*history))) == Dispatch(FIX, HEAD)


def test_attempts_are_per_action_and_per_item():
    history = {pr_key(7): legs(FIX, 1, 2, 3), pr_key(8): legs(REVIEW, 1, 2, 3)}
    assert pr_state(pr(7), local(history)) == Dispatch(REVIEW, HEAD)


@pytest.mark.parametrize(
    "change",
    [
        {"comments": (human(10),)},
        {"reviews": (review("COMMENTED", 10, author="alice"),)},
        {"pushes": (Push("alice", iso(10)),)},
    ],
)
def test_a_non_harness_comment_review_or_push_resets_attempts(change):
    loc = local(pr_legs(*legs(FIX, 1, 2, 3, 11)))
    assert pr_state(pr(ci=RED, **change), loc) == Dispatch(FIX, HEAD)


@pytest.mark.parametrize(
    "change",
    [
        {"comments": (Comment("fix-bot", f"{H.marker} opened", iso(10)),)},
        {"comments": (Comment("ci[bot]", "coverage", iso(10)),)},
        {"comments": (Comment("alice", "hm", "not-a-time"),)},
        {"reviews": (review("CHANGES_REQUESTED", 10, head="old"),)},
        {"pushes": (Push("fix-bot", iso(10)),)},
        {"pushes": (Push("Fix-Bot[bot] ", iso(10)),)},
        {"pushes": (Push("alice", ""),)},
        {"pushes": (Push("", iso(10)),)},  # GitHub could not attribute the commit
        {"pushes": (Push("  ", iso(10)),)},
    ],
)
def test_harness_bot_or_untimed_events_do_not_reset(change):
    loc = local(pr_legs(*legs(FIX, 1, 2, 3)))
    assert pr_state(pr(ci=RED, **change), loc) == NeedsHuman(StopReason.ATTEMPTS, iso(3), FIX)


def test_attempts_after_the_latest_reset_still_reach_the_cap():
    loc = local(pr_legs(*legs(FIX, 1, 2, 11, 12, 13)))
    assert pr_state(pr(ci=RED, comments=(human(10),)), loc) == NeedsHuman(StopReason.ATTEMPTS, iso(13), FIX)


# ── Attempt runs (Q21) ───────────────────────────────────────────────────────


def seq(*steps: tuple[AgentKind, str] | tuple[AgentKind, str, bool]) -> dict:
    """Legs one minute apart, oldest first: (kind, head[, machine])."""
    return pr_legs(*(LegEnd(s[0], at(i), s[2] if len(s) > 2 else False, head=s[1]) for i, s in enumerate(steps)))


def test_fix_ci_red_fix_red_three_times_caps():
    loc = local(seq((FIX, "a"), (FIX, "b"), (FIX, HEAD)))
    assert pr_state(pr(ci=RED), loc) == NeedsHuman(StopReason.ATTEMPTS, iso(2), FIX)


def test_alternating_successful_rebases_and_reviews_never_cap():
    rebase = AgentKind.REBASE
    loc = local(seq((rebase, "a"), (REVIEW, "b"), (rebase, "b"), (REVIEW, "c"), (rebase, "c")))
    assert pr_state(pr(mergeable="CONFLICTING"), loc) == Dispatch(rebase, HEAD)
    assert pr_state(pr(), loc) == Dispatch(REVIEW, HEAD)


def test_reviews_of_four_different_heads_never_cap():
    loc = local(seq((REVIEW, "a"), (REVIEW, "b"), (REVIEW, "c"), (REVIEW, "d")))
    assert pr_state(pr(), loc) == Dispatch(REVIEW, HEAD)


def test_three_reviews_of_the_same_head_cap():
    loc = local(seq((REVIEW, "a"), (REVIEW, HEAD), (REVIEW, HEAD), (REVIEW, HEAD)))
    assert pr_state(pr(), loc) == NeedsHuman(StopReason.ATTEMPTS, iso(3), REVIEW)


def test_machine_legs_inside_a_run_neither_break_nor_count():
    loc = local(seq((FIX, "a"), (FIX, "a", True), (REVIEW, "a", True), (FIX, "b"), (FIX, "b", True), (FIX, HEAD)))
    assert pr_state(pr(ci=RED), local(loc.legs, now=at(100))) == NeedsHuman(StopReason.ATTEMPTS, iso(5), FIX)
    two = local(seq((FIX, "a"), (FIX, "a", True), (FIX, HEAD)), now=at(100))
    assert pr_state(pr(ci=RED), two) == Dispatch(FIX, HEAD)


def test_another_acts_agent_leg_ends_the_run():
    loc = local(seq((FIX, "a"), (FIX, "b"), (AgentKind.ADDRESS_REVIEW, "b"), (FIX, HEAD)))
    assert pr_state(pr(ci=RED), loc) == Dispatch(FIX, HEAD)


def test_leg_end_head_must_be_a_string():
    with pytest.raises(ValueError):
        LegEnd(FIX, T0, False, head=None)  # type: ignore[arg-type]


# ── Human CHANGES_REQUESTED (Q22) ────────────────────────────────────────────

APPROVED_BY_BOT = review("APPROVED", 5)


def test_human_change_request_on_head_dispatches_address_review_before_bot_review():
    assert pr_state(pr(reviews=(review("CHANGES_REQUESTED", 1, author="alice"),))) == Dispatch(ADDR, HEAD)
    assert pr_state(pr(reviews=(APPROVED_BY_BOT, review("CHANGES_REQUESTED", 6, author="alice")))) == Dispatch(
        ADDR, HEAD
    )


def test_human_change_request_driven_address_review_counts_attempts_not_bot_rounds():
    bot_rounds = tuple(review("CHANGES_REQUESTED", m, head=f"old{m}") for m in (1, 2, 3))
    view = pr(reviews=(*bot_rounds, review("CHANGES_REQUESTED", 4, author="alice")))
    assert pr_state(view) == Dispatch(ADDR, HEAD)  # three bot rounds, yet no rounds stop
    after_review = local({pr_key(7): tuple(LegEnd(ADDR, at(10 + m), False, head=HEAD) for m in range(3))})
    assert pr_state(view, after_review) == NeedsHuman(StopReason.ATTEMPTS, iso(12), ADDR)


def test_human_change_request_on_an_older_head_blocks_merge():
    view = pr(reviews=(review("CHANGES_REQUESTED", 1, author="alice", head="old"), APPROVED_BY_BOT))
    assert pr_state(view) == Wait(WaitReason.HUMAN_CHANGES_REQUESTED)


def test_human_change_request_on_an_older_head_still_lets_the_bot_review_the_new_head():
    view = pr(reviews=(review("CHANGES_REQUESTED", 1, author="alice", head="old"),))
    assert pr_state(view) == Dispatch(REVIEW, HEAD)


@pytest.mark.parametrize("clearing", ["APPROVED", "DISMISSED"])
def test_a_later_approval_or_dismissal_by_the_same_human_clears_it(clearing):
    view = pr(
        reviews=(
            review("CHANGES_REQUESTED", 1, author="alice", head="old"),
            review(clearing, 2, author="Alice", head="old"),
            APPROVED_BY_BOT,
        )
    )
    assert pr_state(view) == Merge(HEAD)


def test_another_humans_approval_or_a_comment_does_not_clear_it():
    view = pr(
        reviews=(
            review("CHANGES_REQUESTED", 1, author="alice", head="old"),
            review("APPROVED", 2, author="bob"),
            review("COMMENTED", 3, author="alice"),
            APPROVED_BY_BOT,
        )
    )
    assert pr_state(view) == Wait(WaitReason.HUMAN_CHANGES_REQUESTED)


def test_harness_reviewers_are_not_humans():
    view = pr(reviews=(review("CHANGES_REQUESTED", 1, author="fix-bot", head="old"), APPROVED_BY_BOT))
    assert pr_state(view) == Merge(HEAD)


# ── Review rounds (Q2) ───────────────────────────────────────────────────────


def test_third_bot_change_request_stops_instead_of_addressing():
    rounds = tuple(review("CHANGES_REQUESTED", m, head=h) for m, h in ((1, "a"), (2, "b"), (3, HEAD)))
    assert pr_state(pr(reviews=rounds[1:])) == Dispatch(ADDR, HEAD)
    assert pr_state(pr(reviews=rounds)) == NeedsHuman(StopReason.REVIEW_ROUNDS, "-")


def test_rounds_do_not_block_an_approved_head():
    rounds = tuple(review("CHANGES_REQUESTED", m, head=f"h0{m}") for m in (1, 2, 3)) + (review("APPROVED", 4),)
    assert pr_state(pr(reviews=rounds)) == Merge(HEAD)


def test_human_engagement_resets_review_rounds():
    rounds = tuple(review("CHANGES_REQUESTED", m, head=h) for m, h in ((1, "a"), (2, "b"), (3, HEAD)))
    assert pr_state(pr(reviews=rounds, comments=(human(2.5),))) == Dispatch(ADDR, HEAD)


def test_untimed_bot_change_requests_still_count_as_rounds():
    rounds = tuple(Review("CHANGES_REQUESTED", "review-bot", submitted_at="garbage", commit_id=HEAD) for _ in range(3))
    assert pr_state(pr(reviews=rounds, comments=(human(10),))) == NeedsHuman(StopReason.REVIEW_ROUNDS, "-")


# ── Backoff (Q16), lease, caps (Q7) ──────────────────────────────────────────


def test_machine_failure_backs_off_then_dispatches():
    history = pr_legs(*legs(FIX, 0, machine=True))
    assert pr_state(pr(ci=RED), local(history, now=at(0.5))) == Backoff(FIX, at(1))
    assert pr_state(pr(ci=RED), local(history, now=at(2))) == Dispatch(FIX, HEAD)
    assert actions({pr_key(7): Backoff(FIX, at(1))}, H) == ()


def test_lease_on_the_item_or_its_issue_is_busy_and_silent():
    assert pr_state(pr(ci=RED), local(leases=frozenset({pr_key(7)}))) == Busy()
    assert pr_state(pr(ci=RED), local(leases=frozenset({issue_key(3)}))) == Busy()
    assert actions({pr_key(7): Busy()}, H) == ()


def test_short_daily_headroom_is_a_silent_wait_for_paid_legs_only():
    states = derive(WorldState(issues=(issue(3),)), local(headroom=False), H)
    assert states == {issue_key(3): Wait(WaitReason.HEADROOM)}
    assert actions(states, H) == ()


def test_lifetime_cap_needs_a_human_on_the_pr_stage_too():
    assert pr_state(pr(), local(lifetime_capped=frozenset({3}))) == NeedsHuman(StopReason.LIFETIME_CAP, "-")


def test_a_reply_cannot_release_a_lifetime_cap():
    view = labelled([stop(StopReason.LIFETIME_CAP, 10), human(20)])
    assert pr_state(view, local(lifetime_capped=frozenset({3}))) == Wait(WaitReason.NEEDS_HUMAN)


def test_a_reply_releases_a_lifetime_stop_once_the_cap_is_raised():
    view = labelled([stop(StopReason.LIFETIME_CAP, 10), human(20)])
    assert pr_state(view, local()) == Release()


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        (pr(reviews=(review("APPROVED", 1),)), Merge(HEAD)),
        (pr(merge_state="BEHIND", reviews=(review("APPROVED", 1),)), Refresh(HEAD)),
    ],
)
def test_the_lifetime_cap_never_blocks_free_work(view, expected):
    assert pr_state(view, local(lifetime_capped=frozenset({3}))) == expected


def test_the_lifetime_cap_never_blocks_opening_a_pushed_branch():
    world = WorldState(issues=(issue(3),), branches=(Branch("fix/3-slug", iso(1)),))
    assert one(world, local(lifetime_capped=frozenset({3}))) == OpenPR("fix/3-slug")


# ── Precedence (conflicting signals) ─────────────────────────────────────────

WF = frozenset({".github/workflows/ci.yml"})


@pytest.mark.parametrize(
    ("view", "loc", "expected"),
    [
        (labelled(changed_files=WF), local(leases=frozenset({pr_key(7)}), lifetime_capped=frozenset({3})), Busy()),
        (pr(changed_files=WF), local(lifetime_capped=frozenset({3})), NeedsHuman(StopReason.WORKFLOW_PATH, "-")),
        (labelled([stop(StopReason.ATTEMPTS, 1)], changed_files=WF), local(), Wait(WaitReason.NEEDS_HUMAN)),
        (
            pr(ci=RED),
            local(pr_legs(*legs(FIX, 1, 2, 3)), lifetime_capped=frozenset({3})),
            NeedsHuman(StopReason.ATTEMPTS, iso(3), FIX),
        ),
        (pr(ci=RED), local(lifetime_capped=frozenset({3})), NeedsHuman(StopReason.LIFETIME_CAP, "-")),
        (
            pr(ci=RED, changed_files=WF),
            local(pr_legs(*legs(FIX, 1, 2, 3))),
            NeedsHuman(StopReason.WORKFLOW_PATH, iso(3)),
        ),
        (
            pr(ci=RED),
            local(pr_legs(*legs(FIX, 1, 2, 3) + legs(FIX, 4, machine=True)), now=at(4.5)),
            NeedsHuman(StopReason.ATTEMPTS, iso(4), FIX),
        ),
        (pr(ci=RED), local(pr_legs(*legs(FIX, 1, 2, 3)), headroom=False), NeedsHuman(StopReason.ATTEMPTS, iso(3), FIX)),
        (pr(ci=RED), local(pr_legs(*legs(FIX, 0, machine=True)), now=at(0.5), headroom=False), Backoff(FIX, at(1))),
        (
            pr(ci=RED, mergeable="CONFLICTING", reviews=(review("APPROVED", 1),)),
            local(),
            Dispatch(AgentKind.REBASE, HEAD),
        ),
        (pr(ci=RED, merge_state="BEHIND"), local(), Dispatch(FIX, HEAD)),
        (pr(merge_state="BEHIND", reviews=(review("APPROVED", 1),)), local(), Refresh(HEAD)),
    ],
)
def test_precedence(view, loc, expected):
    assert pr_state(view, loc) == expected


# ── Policy contract ──────────────────────────────────────────────────────────


def test_stop_comment_key_is_stable_per_episode_and_new_per_leg_end():
    def key(*minutes):
        states = derive(WorldState(prs=(pr(ci=RED),)), local(pr_legs(*legs(FIX, *minutes))), H)
        return actions(states, H)[1].key

    assert key(1, 2, 3) == key(1, 2, 3)
    assert key(1, 2, 3) != key(1, 2, 4)


def test_policy_stop_comment_is_read_back_as_the_stop():
    states = derive(WorldState(prs=(pr(ci=RED),)), local(pr_legs(*legs(FIX, 1, 2, 3))), H)
    comment = actions(states, H)[1]
    posted = Comment("fix-bot", comment.body, iso(10))
    assert pr_state(labelled([posted])) == Wait(WaitReason.NEEDS_HUMAN)
    assert pr_state(labelled([posted, human(20)])) == Release()


def test_actions_are_ordered_issues_first_then_by_number():
    world = WorldState(issues=(issue(9), issue(2)), prs=(pr(5, 30, ci=RED), pr(4, 31, ci=RED)))
    numbers = [(a.kind, a.number) for a in actions(derive(world, local(), H), H)]
    assert numbers == [(AgentKind.RESOLVE, 2), (AgentKind.RESOLVE, 9), (FIX, 4), (FIX, 5)]


def test_policy_refuses_resolve_on_a_pr():
    with pytest.raises(ValueError):
        actions({pr_key(7): Dispatch(AgentKind.RESOLVE)}, H)


# ── Malformed and huge inputs ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "build",
    [
        lambda: Harness("", "review-bot"),
        lambda: Harness("fix-bot", "  "),
        lambda: Harness("fix-bot", "review-bot", marker="issue-agent"),
        lambda: Harness("fix-bot", "review-bot", marker="<!---->"),
        lambda: Harness("fix-bot", "review-bot", marker="<!--" + "x" * 500 + "-->"),
        lambda: Local(now=datetime(2026, 10, 1)),
        lambda: Local(now=NOW, legs={7: ()}),
        lambda: Local(now=NOW, legs={pr_key(7): [LegEnd(FIX, T0, False)]}),
        lambda: Local(now=NOW, leases=frozenset({7})),
        lambda: Local(now=NOW, headroom=1),
        lambda: Local(now=NOW, lifetime_capped=frozenset({0})),
        lambda: ItemKey(Target.PR, 0),
        lambda: ItemKey(Target.PR, True),
        lambda: Dispatch(AgentKind.RESOLVE, "h"),
        lambda: Dispatch(FIX, ""),
        lambda: Merge(""),
        lambda: NeedsHuman(StopReason.ATTEMPTS, "-"),
        lambda: NeedsHuman(StopReason.WORKFLOW_PATH, "-", FIX),
        lambda: NeedsHuman(StopReason.WORKFLOW_PATH, ""),
        lambda: NeedsHuman(StopReason.QUESTION, "-"),
        lambda: NeedsHuman(StopReason.WORKFLOW_PATH, "-", question="why?"),
        lambda: Backoff(FIX, datetime(2026, 10, 1)),
        lambda: MergePullRequest(7, ""),
    ],
)
def test_malformed_values_are_rejected(build):
    with pytest.raises(ValueError):
        build()


def test_local_legs_cannot_be_mutated_after_construction():
    source = {pr_key(7): legs(FIX, 1)}
    loc = local(source)
    source[pr_key(7)] = legs(FIX, 1, 2, 3)
    assert pr_state(pr(ci=RED), loc) == Dispatch(FIX, HEAD)


def test_ten_thousand_comments_with_a_late_reply_release():
    noise = tuple(human(m) for m in range(10_000))
    view = labelled([*noise, stop(StopReason.ATTEMPTS, 20_000), human(20_001)])
    assert pr_state(view) == Release()


def test_ten_thousand_old_comments_do_not_release():
    view = labelled([*(human(m) for m in range(10_000)), stop(StopReason.ATTEMPTS, 20_000)])
    assert pr_state(view) == Wait(WaitReason.NEEDS_HUMAN)


def test_ten_thousand_machine_legs_do_not_stop_and_back_off_at_most_an_hour():
    history = pr_legs(*legs(FIX, *range(10_000), machine=True))
    assert pr_state(pr(ci=RED), local(history, now=at(9_999))) == Backoff(FIX, at(9_999 + 60))


def test_many_items_derive_independently():
    world = WorldState(issues=tuple(issue(n) for n in range(1, 2001)))
    states = derive(world, local({issue_key(5): legs(AgentKind.RESOLVE, 1, 2, 3)}), H)
    assert len(states) == 2000
    assert states[issue_key(5)] == NeedsHuman(StopReason.ATTEMPTS, iso(3), AgentKind.RESOLVE)
    assert states[issue_key(6)] == Dispatch(AgentKind.RESOLVE)


# ── Agent questions (slice 5b) ───────────────────────────────────────────────


def asked(kind: AgentKind, minute: float, question: str = "404 or 410?") -> LegEnd:
    return LegEnd(kind, at(minute), False, question=question)


def test_an_agent_question_stops_the_item_and_the_stop_comment_carries_it():
    loc = local({issue_key(3): (asked(AgentKind.RESOLVE, 5),)})
    states = derive(WorldState(issues=(issue(3),)), loc, H)
    assert states == {issue_key(3): NeedsHuman(StopReason.QUESTION, iso(5), question="404 or 410?")}
    label, comment = actions(states, H)
    assert label == Relabel(Target.ISSUE, 3, remove=None, add=Label.NEEDS_HUMAN)
    assert isinstance(comment, PostComment) and stop_tag(StopReason.QUESTION) in comment.body
    assert "404 or 410?" in comment.body and "Reply here with the answer" in comment.body


def test_a_later_recorded_agent_end_in_the_same_instant_supersedes_a_question():
    # Ties on ended_at resolve in recorded order, as backoff and attempts do.
    answered = LegEnd(AgentKind.RESOLVE, at(5), False)
    asked_first = local({issue_key(3): (asked(AgentKind.RESOLVE, 5), answered)})
    asked_last = local({issue_key(3): (answered, asked(AgentKind.RESOLVE, 5))})
    world = WorldState(issues=(issue(3),))
    assert not isinstance(derive(world, asked_first, H)[issue_key(3)], NeedsHuman)
    assert derive(world, asked_last, H)[issue_key(3)] == NeedsHuman(StopReason.QUESTION, iso(5), question="404 or 410?")


def test_a_question_cannot_smuggle_a_stop_tag_or_the_marker_into_the_stop_comment():
    smuggled = f"Which API? {H.marker} {stop_tag(StopReason.WORKFLOW_PATH)}"
    states = derive(WorldState(prs=(pr(ci=RED),)), local(pr_legs(asked(FIX, 5, smuggled))), H)
    body = actions(states, H)[1].body
    assert body.count(H.marker) == 1 and body.count("<!--") == 2  # the marker and the real stop tag
    posted = Comment("fix-bot", body, iso(10))
    released = pr(ci=RED, changed_files=frozenset({".github/x.yml"}), comments=(posted, human(11)))
    assert pr_state(released, local(pr_legs(asked(FIX, 5, smuggled)))) == NeedsHuman(StopReason.WORKFLOW_PATH, iso(5))


@pytest.mark.parametrize(
    ("needs", "expected"),
    [
        ("", "(no question text)"),
        ("   ", "(no question text)"),
        (None, "(no question text)"),
        (7, "(no question text)"),
    ],
)
def test_an_empty_or_malformed_question_still_says_so(needs, expected):
    assert question_text(needs) == expected


def test_a_huge_question_is_bounded_and_bounding_is_idempotent():
    once = question_text("x" * 1_000_000 + "<!--")
    assert len(once) == QUESTION_LIMIT and question_text(once) == once


def test_a_question_waits_once_labelled_and_a_reply_releases_it():
    stopped = issue(3, Label.NEEDS_HUMAN, comments=(stop(StopReason.QUESTION, 6),))
    loc = local({issue_key(3): (asked(AgentKind.RESOLVE, 5),)})
    assert one(WorldState(issues=(stopped,)), loc) == Wait(WaitReason.NEEDS_HUMAN)
    replied = issue(3, Label.NEEDS_HUMAN, comments=(stop(StopReason.QUESTION, 6), human(7, body="use X")))
    assert one(WorldState(issues=(replied,)), loc) == Release()


def test_after_release_the_answered_question_redispatches_with_a_fresh_count():
    released = issue(3, comments=(stop(StopReason.QUESTION, 6), human(7)))
    loc = local({issue_key(3): (*legs(AgentKind.RESOLVE, 1, 2), asked(AgentKind.RESOLVE, 5))})
    assert one(WorldState(issues=(released,)), loc) == Dispatch(AgentKind.RESOLVE)


def test_a_later_agent_leg_supersedes_an_old_question():
    loc = local(pr_legs(asked(FIX, 1), *legs(FIX, 2)))
    assert pr_state(pr(ci=RED), loc) == Dispatch(FIX, HEAD)


def test_a_machine_end_after_a_question_does_not_hide_it():
    loc = local(pr_legs(asked(FIX, 1), *legs(FIX, 2, machine=True)))
    assert pr_state(pr(ci=RED), loc) == NeedsHuman(StopReason.QUESTION, iso(2), question="404 or 410?")


@pytest.mark.parametrize(
    "bad",
    [
        dict(machine=True, question="why?"),
        dict(machine=False, question=""),
        dict(machine=False, question=1),
        dict(machine=False, started_at=at(10)),  # after the end
        dict(machine=False, started_at=datetime(2026, 10, 1, 11, 0)),  # naive
        dict(machine=False, started_at="2026-10-01T11:00:00Z"),
    ],
)
def test_leg_end_rejects_contradictory_question_or_window(bad):
    machine = bad.pop("machine")
    with pytest.raises(ValueError):
        LegEnd(FIX, at(5), machine, **bad)


# ── Own comments inside a leg (slice 5b) ─────────────────────────────────────


def windowed(kind: AgentKind, start: float, end: float) -> LegEnd:
    return LegEnd(kind, at(end), False, started_at=at(start))


def test_the_agents_own_unmarked_comment_inside_its_leg_is_not_a_reset():
    own = Comment("fix-bot", "Acceptance criteria: ...", iso(2.5))
    loc = local(pr_legs(windowed(FIX, 0.5, 1), windowed(FIX, 2, 3), windowed(FIX, 4, 5)))
    assert pr_state(pr(ci=RED, comments=(own,)), loc) == NeedsHuman(StopReason.ATTEMPTS, iso(5), FIX)


def test_the_same_comment_outside_every_leg_window_resets():
    between = Comment("alice", "try the other approach", iso(3.5))
    loc = local(pr_legs(windowed(FIX, 0.5, 1), windowed(FIX, 2, 3), windowed(FIX, 4, 5)))
    assert pr_state(pr(ci=RED, comments=(between,)), loc) == Dispatch(FIX, HEAD)


# ── One definition of a human comment (#342) ────────────────────────────────


def _bodies(comments, legs_=()) -> list[str]:
    return [c.body for _, c in human_comments(comments, H, legs_)]


def test_342_the_agents_unmarked_fix_bot_comment_inside_a_machine_ended_leg_is_not_human():
    # tauri #1102: the leg posted progress as FIX_BOT (the operator's login),
    # then hit the wall cap. Its comment is not the human's answer.
    capped = LegEnd(FIX, at(10), True, started_at=at(5))
    own = Comment("fix-bot", "Progress: tried mocking the clock", iso(7))
    assert human_comments([own], H, [capped]) == ()


def test_342_the_operator_as_fix_bot_replying_outside_every_leg_is_human_oldest_first():
    legs_ = [windowed(FIX, 1, 2), LegEnd(FIX, at(6), True, started_at=at(4))]
    comments = [human(8, author="fix-bot", body="after"), human(3, author="FIX-BOT ", body="between"),
                human(5, author="fix-bot", body="inside")]
    assert _bodies(comments, legs_) == ["between", "after"]


@pytest.mark.parametrize("stamp", ["", "yesterday", "2026-13-40T00:00:00Z", "x" * 100_000])
def test_a_comment_whose_time_is_absent_or_malformed_is_never_human(stamp):
    assert human_comments([Comment("alice", "ok", stamp)], H, ()) == ()


def test_a_leg_that_never_launched_has_no_window():
    refused = LegEnd(FIX, at(10), True)  # started_at None: refused before launch
    assert _bodies([human(9, body="reply")], [refused]) == ["reply"]


def test_harness_and_bot_comments_are_never_human():
    marked = Comment("fix-bot", f"{H.marker} stop", iso(1))
    assert human_comments([marked, human(2, author="ci[bot]")], H, ()) == ()


def test_window_end_is_inclusive():
    assert human_comments([human(5)], H, [windowed(FIX, 4, 5)]) == ()


def test_ten_thousand_comments_against_many_legs():
    legs_ = [windowed(FIX, 2 * k, 2 * k + 1) for k in range(50)]
    comments = [human(m + 0.5) for m in range(10_000)]
    # Inside: 2k+0.5 for k < 50; every other minute is outside.
    assert len(human_comments(comments, H, legs_)) == 10_000 - 50


def test_342_a_mid_leg_comment_cannot_release_a_stop_but_a_later_reply_does():
    capped = LegEnd(FIX, at(10), True, started_at=at(5))
    reply = Comment("fix-bot", "Mock the clock.", iso(12))
    assert pr_state(labelled([stop(StopReason.ATTEMPTS, 11), reply]), local(pr_legs(capped))) == Release()
    inside = labelled([human(7, author="fix-bot"), stop(StopReason.ATTEMPTS, 11)])
    assert pr_state(inside, local(pr_legs(capped))) == Wait(WaitReason.NEEDS_HUMAN)


def test_a_comment_at_the_leg_start_instant_is_outside_the_window():
    # The window is (start, end]: a human comment the leg was dispatched on is a reset.
    edge = Comment("alice", "go", iso(4))
    loc = local(pr_legs(windowed(FIX, 0.5, 1), windowed(FIX, 2, 3), windowed(FIX, 4, 5)))
    assert pr_state(pr(ci=RED, comments=(edge,)), loc) == Dispatch(FIX, HEAD)


# ── Publication: the agent pushes, the harness opens (Q11, slice 5b) ─────────


def test_a_pushed_branch_without_a_pr_opens_one_for_free():
    world = WorldState(issues=(issue(3),), branches=(Branch("fix/3-slug", iso(1)),))
    loc = local({issue_key(3): legs(AgentKind.RESOLVE, 1, 2, 3)}, headroom=False)
    states = derive(world, loc, H)
    assert states == {issue_key(3): OpenPR("fix/3-slug")}
    assert actions(states, H) == (OpenPullRequest(3, "fix/3-slug"),)


def test_the_latest_committed_branch_wins_and_untimed_ones_lose():
    branches = (Branch("fix/3-a", iso(5)), Branch("fix/3-b", iso(9)), Branch("fix/3-c", "not-a-time"))
    assert one(WorldState(issues=(issue(3),), branches=branches)) == OpenPR("fix/3-b")


def test_another_issues_branch_does_not_open_a_pr():
    world = WorldState(issues=(issue(3),), branches=(Branch("fix/4-slug", iso(1)), Branch("feat/3-x", iso(1))))
    assert one(world) == Dispatch(AgentKind.RESOLVE)


def test_a_stopped_issue_with_a_pushed_branch_waits_for_the_human():
    stopped = issue(3, Label.NEEDS_HUMAN, comments=(stop(StopReason.ATTEMPTS, 4),))
    world = WorldState(issues=(stopped,), branches=(Branch("fix/3-slug", iso(1)),))
    assert one(world) == Wait(WaitReason.NEEDS_HUMAN)


def test_a_running_leg_defers_publication_until_it_returns():
    world = WorldState(issues=(issue(3),), branches=(Branch("fix/3-slug", iso(1)),))
    assert one(world, local(leases=frozenset({issue_key(3)}))) == Busy()


def test_a_merged_harness_pr_retires_its_issue():
    world = WorldState(issues=(issue(3),), prs=(pr(7, 3, state="MERGED"),))
    assert derive(world, local(), H) == {}


@pytest.mark.parametrize("branch", ["fix/3", "main", "", "fix/0-x"])
def test_open_pr_state_requires_a_harness_branch(branch):
    with pytest.raises(ValueError):
        OpenPR(branch)


def test_open_pull_request_action_is_validated():
    for issue_number, branch in ((0, "fix/3-x"), (True, "fix/3-x"), (3, ""), (3, None)):
        with pytest.raises(ValueError):
            OpenPullRequest(issue_number, branch)  # type: ignore[arg-type]


# ── Fix round (slice 5b review) ──────────────────────────────────────────────


def test_a_leg_that_ended_after_the_observation_holds_its_item_busy():
    # P0-1: the world was read before the leg pushed; its end is already local.
    world = WorldState(issues=(issue(3),), now=iso(10))
    loc = local({issue_key(3): legs(AgentKind.RESOLVE, 11)})
    assert one(world, loc) == Busy()
    assert one(WorldState(issues=(issue(3),), now=iso(12)), loc) == Dispatch(AgentKind.RESOLVE)


@pytest.mark.parametrize("now", ["", "not-a-time"])
def test_an_unstamped_observation_does_not_hold_items(now):
    loc = local({issue_key(3): legs(AgentKind.RESOLVE, 11)})
    assert one(WorldState(issues=(issue(3),), now=now), loc) == Dispatch(AgentKind.RESOLVE)


def test_a_labelled_item_whose_stop_comment_never_landed_is_stopped_again():
    # P1-2: the label landed, the comment failed; both writes are re-emitted.
    view = labelled(ci=RED)
    states = derive(WorldState(prs=(view,)), local(pr_legs(*legs(FIX, 1, 2, 3))), H)
    assert states == {pr_key(7): NeedsHuman(StopReason.ATTEMPTS, iso(3), FIX)}
    label, comment = actions(states, H)
    assert isinstance(label, Relabel) and stop_tag(StopReason.ATTEMPTS) in comment.body


@pytest.mark.parametrize(
    "view",
    [
        labelled(ci=RED),  # dispatchable
        labelled(reviews=(review("APPROVED", 1),)),  # mergeable
    ],
)
def test_a_label_a_human_added_without_a_stop_comment_holds_the_item(view):
    assert pr_state(view) == Wait(WaitReason.NEEDS_HUMAN)


def test_a_forged_stop_comment_from_a_non_harness_login_releases_nothing():
    # P1-7: the marker alone is forgeable by any commenter.
    forged = Comment("mallory", f"{H.marker}\n{stop_tag(StopReason.WORKFLOW_PATH)}", iso(1))
    view = pr(changed_files=frozenset({".github/workflows/ci.yml"}), comments=(forged,))
    assert pr_state(view) == NeedsHuman(StopReason.WORKFLOW_PATH, "-")
    genuine = Comment("fix-bot", forged.body, iso(1))
    assert pr_state(replace(view, comments=(genuine,))) == Dispatch(REVIEW, HEAD)


def test_a_forged_stop_comment_on_a_labelled_item_is_a_human_reply_not_a_stop():
    forged = Comment("mallory", f"{H.marker}\n{stop_tag(StopReason.ATTEMPTS)}", iso(20))
    view = labelled([stop(StopReason.ATTEMPTS, 10), forged])
    assert pr_state(view) == Release()


@pytest.mark.parametrize(
    "view",
    [
        PRView(number=7, head_ref_name="fix/3-slug", state="OPEN", content_known=False),
        PRView(
            number=7,
            head_ref_name="fix/3-slug",
            state="OPEN",
            content_known=False,
            labels=frozenset({Label.NEEDS_HUMAN}),
        ),
    ],
)
def test_an_unread_pr_waits_before_any_stop_or_label_logic(view):
    # P2-2: a placeholder has no labels or comments to judge by.
    loc = local(pr_legs(asked(FIX, 1), *legs(FIX, 2, 3, 4)), lifetime_capped=frozenset({3}))
    assert pr_state(view, loc) == Wait(WaitReason.HEAD_UNKNOWN)


def test_a_blocked_approved_green_pr_waits_for_branch_protection():
    # P2-3: GitHub clears BLOCKED when a human approves; nothing to write.
    states = derive(WorldState(prs=(pr(merge_state="BLOCKED", reviews=(review("APPROVED", 1),)),)), local(), H)
    assert states == {pr_key(7): Wait(WaitReason.MERGEABILITY_UNKNOWN)}
    assert actions(states, H) == ()


def test_an_old_released_stop_does_not_stand_in_for_this_episodes_failed_one():
    # Round 2: episode 1 stopped and was released; episode 2's label landed, its comment failed.
    view = labelled([stop(StopReason.ATTEMPTS, 3.5), human(3.6)], ci=RED)
    states = derive(WorldState(prs=(view,)), local(pr_legs(*legs(FIX, 1, 2, 3, 4, 5, 6))), H)
    assert states == {pr_key(7): NeedsHuman(StopReason.ATTEMPTS, iso(6), FIX)}


def test_an_old_lifetime_stop_does_not_pin_a_different_current_stop():
    view = labelled([stop(StopReason.LIFETIME_CAP, 0.5), human(0.6)], ci=RED)
    loc = local(pr_legs(*legs(FIX, 1, 2, 3)), lifetime_capped=frozenset({3}))
    assert pr_state(view, loc) == NeedsHuman(StopReason.ATTEMPTS, iso(3), FIX)
