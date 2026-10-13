"""The dashboard read model: board status from derived states, rail and
history from legs, and the legs metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain import dashboard as dash
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.state import (
    Backoff,
    Dispatch,
    Harness,
    Merge,
    NeedsHuman,
    Release,
    StopReason,
    Wait,
    WaitReason,
    stop_tag,
)
from issue_agent.domain.world import Comment, PRView

T0 = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
HARNESS = Harness(fix_bot="fixbot", review_bot="reviewbot")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def stamp(minutes: float) -> str:
    return at(minutes).isoformat()


def stop(reason: StopReason, minutes: float, author: str = "fixbot", text: str = "why") -> Comment:
    return Comment(author, f"{HARNESS.marker}\n{stop_tag(reason)}\n**needs-human**: {text}", stamp(minutes))


def leg(
    kind: str = "RESOLVE",
    minutes: float = 0,
    *,
    ended: float | None = None,
    state: str = "ended",
    cls: str = "agent",
    cost: float | None = 1.0,
    verdict: str = "",
    number: int = 0,
    issue: int = 7,
    basis: str = "provider_reported",
    leg_id: str | None = None,
    role: str | None = None,
) -> dash.LegRow:
    running = state in ("reserved", "running")
    ended = minutes + 1 if ended is None else ended
    return dash.LegRow(
        leg_id=leg_id or f"{kind}-{minutes}",
        role=role or ("review" if kind == "REVIEW" else "fix"),
        kind=kind,
        issue=issue,
        number=number,
        head="h1",
        runtime="claude",
        state=state,
        reserved_at=at(minutes),
        started_at=at(minutes) if state != "reserved" else None,
        ended_at=None if running else at(ended),
        reserved_usd=10.0,
        charged_usd=None if running or state == "released" else cost,
        basis="" if running or state == "released" else basis,
        leg_class="" if running else cls,
        verdict=verdict,
    )


def status(state, *, comments=(), pr=None, legs=(), target="issue", last_end=None):
    """`last_end` is derive's item boundary; by default the newest end of `legs`."""
    if last_end is None:
        last_end = max((x.ended_at for x in legs if x.ended_at is not None), default=None)
    return dash.board_status(
        state, target=target, comments=comments, pr=pr, legs=tuple(legs), last_end=last_end, harness=HARNESS
    )


# ── needs you ────────────────────────────────────────────────────────────────


def test_a_question_stop_is_needs_you_with_the_question_and_answered_on_github():
    comments = [stop(StopReason.QUESTION, 5, text="Which API should I use?")]
    board = status(Wait(WaitReason.NEEDS_HUMAN), comments=comments, legs=[leg(ended=2)])

    assert isinstance(board, dash.NeedsYou)
    assert board.reason == "question" and board.target == "issue"
    assert "Which API should I use?" in (board.detail or "")
    assert HARNESS.marker not in (board.detail or "") and "needs-human:" not in (board.detail or "")
    assert board.since_ts == stamp(5)
    # Unblocking would release it unanswered: the row points at the reply instead.
    assert dash.primary_action(board, held=True) == "open"


@pytest.mark.parametrize("reason", [StopReason.ATTEMPTS, StopReason.REVIEW_ROUNDS, StopReason.WORKFLOW_PATH])
def test_a_labelled_stop_names_its_reason_and_offers_unblock(reason):
    board = status(Wait(WaitReason.NEEDS_HUMAN), comments=[stop(reason, 5)], legs=[leg(ended=2)], target="pr")
    assert isinstance(board, dash.NeedsYou)
    assert (board.reason, board.target, dash.primary_action(board, held=True)) == (reason.value, "pr", "unblock")


@pytest.mark.parametrize(
    "comment",
    [
        stop(StopReason.ATTEMPTS, 5, author="mallory"),  # marker forged by a commenter
        Comment("fixbot", f"{stop_tag(StopReason.ATTEMPTS)} without the marker", stamp(5)),
        Comment("fixbot", f"{HARNESS.marker}\n{stop_tag(StopReason.ATTEMPTS)}", "not a time"),
    ],
)
def test_only_a_harness_written_dated_stop_names_the_reason(comment):
    assert status(Wait(WaitReason.NEEDS_HUMAN), comments=[comment]).reason == "labelled"


def test_the_newest_stop_wins():
    comments = [stop(StopReason.ATTEMPTS, 5), stop(StopReason.REVIEW_ROUNDS, 7), stop(StopReason.QUESTION, 6)]
    assert status(Wait(WaitReason.NEEDS_HUMAN), comments=comments).reason == "review-rounds"


def test_a_huge_stop_comment_is_bounded():
    board = status(Wait(WaitReason.NEEDS_HUMAN), comments=[stop(StopReason.QUESTION, 5, text="x" * 50_000)])
    assert len(board.detail or "") <= dash.MESSAGE_CHARS


def test_a_stop_about_to_be_posted_is_already_needs_you():
    board = status(NeedsHuman(StopReason.QUESTION, "-", question="Proceed?"), legs=[leg(ended=3)])
    assert (board.reason, board.detail, board.since_ts) == ("question", "Proceed?", stamp(3))
    attempts = status(NeedsHuman(StopReason.ATTEMPTS, "-", AgentKind.FIX_CI))
    assert attempts.reason == "attempts" and dash.primary_action(attempts, held=True) == "unblock"
    # Policy has not labelled it yet: removing a label that is not there
    # would do nothing, so the row offers to open the item instead.
    assert dash.primary_action(attempts, held=False) == "open"


# ── working and waiting ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state,activity",
    [
        (Dispatch(AgentKind.FIX_CI, "h1"), "queued"),
        (Merge("h1"), "merge"),
        (Release(), "release"),
        (Wait(WaitReason.CI_PENDING), "ci"),
    ],
)
def test_moving_states_are_working(state, activity):
    board = status(state)
    assert isinstance(board, dash.Working) and board.activity == activity


def test_waits_name_what_they_wait_on():
    blocked = PRView(1, merge_state="BLOCKED", mergeable="MERGEABLE")
    computing = PRView(1, merge_state="UNKNOWN", mergeable="UNKNOWN")
    assert status(Wait(WaitReason.MERGEABILITY_UNKNOWN), pr=blocked).on == "branch-protection"
    assert status(Wait(WaitReason.MERGEABILITY_UNKNOWN), pr=computing).on == "github"
    assert status(Wait(WaitReason.HEADROOM)).on == "headroom"
    assert status(Wait(WaitReason.HUMAN_CHANGES_REQUESTED)).on == "human-review"
    assert status(Wait(WaitReason.CI_UNKNOWN)).on == "github"
    base_red = status(Wait(WaitReason.BASE_RED))
    assert isinstance(base_red, dash.Waiting) and base_red.headline == "Base branch red"
    backoff = status(Backoff(AgentKind.RESOLVE, at(30)))
    assert isinstance(backoff, dash.Waiting) and (backoff.on, backoff.until_ts) == ("backoff", stamp(30))


# ── rail and history ─────────────────────────────────────────────────────────


def test_the_rail_counts_legs_and_remembers_a_rewind():
    legs = (
        leg("RESOLVE", 0),
        leg("REVIEW", 2, verdict="REQUEST_CHANGES", number=9),
        leg("FIX_CI", 4, number=9),
        leg("REBASE", 6, number=9),
        leg("RESOLVE", 8, state="released"),
    )
    rail = dash.rail(Dispatch(AgentKind.FIX_CI, "h1"), has_pr=True, legs=legs)
    assert (rail.current, rail.furthest) == ("ci", "review")
    assert (rail.resolve_attempts, rail.review_rounds, rail.ci_runs, rail.steps) == (1, 1, 2, 4)
    assert rail.live is False


def test_history_folds_identical_consecutive_legs_per_stage_visit():
    legs = (
        leg("RESOLVE", 0, cls="machine"),
        leg("RESOLVE", 1, cls="machine"),
        leg("RESOLVE", 2),
        leg("REVIEW", 3, verdict="APPROVE"),
        leg("FIX_CI", 4, state="running"),
    )
    visits = dash.history(legs)
    assert [(v.stage, [(c.kind, c.outcome, c.count) for c in v.steps]) for v in visits] == [
        ("resolve", [("RESOLVE", "failed", 2), ("RESOLVE", "done", 1)]),
        ("review", [("REVIEW", "passed", 1)]),
        ("ci", [("FIX_CI", "running", 1)]),
    ]


def test_carried_legs_are_money_history_not_attempts_failures_or_toasts():
    carried = (
        leg("RESOLVE", 0, cls="", role="carried", leg_id="C1"),
        leg("REVIEW", 1, cls="", role="carried", number=9, leg_id="C2"),
    )
    live = (leg("RESOLVE", 5, leg_id="L1"),)
    legs = carried + live
    rail = dash.rail(Dispatch(AgentKind.RESOLVE), has_pr=False, legs=legs)
    assert (rail.resolve_attempts, rail.review_rounds, rail.steps, rail.furthest) == (1, 0, 1, "resolve")
    assert [[c.outcome for c in v.steps] for v in dash.history(legs)] == [["done"]]
    m = dash.metrics(legs, merged_prs=None, open_items=None, needs_human=None)
    assert (m.machine_legs, m.total_cost_usd) == (0, 3.0)
    # Their legacy kinds name no act: by_kind folds them into one `carried` row.
    assert {(k.kind, k.runs) for k in m.by_kind} == {("carried", 2), ("RESOLVE", 1)}
    assert [n.id for n in dash.notifications(legs, [])] == ["leg:L1:start", "leg:L1:end"]


# ── legs ─────────────────────────────────────────────────────────────────────


def test_a_leg_costs_its_charge_its_reservation_while_running_and_nothing_once_released():
    assert leg(cost=2.5).cost_usd == 2.5
    running = leg(state="running")
    assert running.cost_usd == 10.0 and running.estimated and running.outcome == "running"
    released = leg(state="released")
    assert released.cost_usd == 0.0 and not released.counted
    assert leg(basis="estimate").estimated and not leg().estimated
    assert leg(cls="asked").outcome == "asked" and leg(cls="").outcome == "failed"


def test_metrics_divide_spend_by_merges_and_count_first_pass_reviews():
    legs = [
        leg("RESOLVE", 0, cost=3.0, issue=1),
        leg("REVIEW", 1, cost=1.0, verdict="APPROVE", number=11, issue=1),
        leg("RESOLVE", 2, cost=2.0, issue=2),
        leg("REVIEW", 3, cost=1.0, verdict="REQUEST_CHANGES", number=12, issue=2),
        leg("REVIEW", 5, cost=1.0, verdict="APPROVE", number=12, issue=2),
        leg("FIX_CI", 6, cost=0.0, cls="machine", number=12, issue=2, basis="estimate"),
        leg("RESOLVE", 7, state="released", issue=3),
    ]
    m = dash.metrics(legs, merged_prs=2, open_items=1, needs_human=0)
    assert m.total_cost_usd == 8.0 and m.cost_per_merged_pr_usd == 4.0
    assert (m.first_pass_approvals, m.first_pass_changes, m.first_pass_rate) == (1, 1, 0.5)
    assert m.rounds_per_pr == 1.5
    assert (m.legs, m.machine_legs) == (6, 1)
    assert {(b.basis, b.usd) for b in m.by_basis} == {("provider_reported", 8.0), ("estimate", 0.0)}
    assert {(k.kind, k.runs) for k in m.by_kind} == {("RESOLVE", 2), ("REVIEW", 3), ("FIX_CI", 1)}


def test_metrics_report_unknown_rather_than_zero():
    m = dash.metrics([], merged_prs=None, open_items=None, needs_human=None)
    assert m.cost_per_merged_pr_usd is None and m.first_pass_rate is None and m.rounds_per_pr is None
    assert (
        dash.metrics([leg()], merged_prs=0, open_items=0, needs_human=0).cost_per_merged_pr_usd is None
    )
    assert "merged ?" in dash.metrics_line(m) and "no merged PRs" in dash.metrics_line(m)


def test_a_truncated_merged_list_marks_the_merge_metrics_as_bounds():
    m = dash.metrics(
        [leg(cost=4.0)], merged_prs=2, open_items=0, needs_human=0, merged_prs_partial=True
    )
    assert m.merged_prs_partial and "merged ≥2" in dash.metrics_line(m)
    assert not dash.metrics([], merged_prs=2, open_items=0, needs_human=0).merged_prs_partial


def test_hourly_spend_keeps_empty_hours_and_drops_stamps_outside_the_window():
    legs = [leg(minutes=0, ended=10, cost=2.0), leg(minutes=600, ended=601, cost=5.0)]  # the second is in the future
    buckets = dash.spend_per_hour(legs, at(30), hours=3)
    assert [b.usd for b in buckets] == [0.0, 0.0, 2.0]
    assert dash.spend_per_hour(legs, at(30), hours=0) == ()


def test_label_values_and_frecency():
    assert dash.label_value(["Type:Bug", "impact:high"], ("type:",), exact=("bug",)) == "Bug"
    assert dash.label_value(["bug"], ("type:",), exact=("bug",)) == "bug"
    assert dash.label_value(["impact:high"], ("impact:",)) == "high"
    assert dash.frecency([at(0), at(-60 * 24 * 3), at(-60 * 24 * 60), at(60)], at(0)) == 4.0
