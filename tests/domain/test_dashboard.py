"""The dashboard read model: board status from derived states, rail and
history from legs, and the legs metrics."""

from __future__ import annotations
from dataclasses import replace

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain import dashboard as dash
from issue_agent.domain.actions import ISSUE_ACTS, AgentKind
from issue_agent.domain.state import (
    AttemptsStop,
    Backoff,
    Blocked,
    Dispatch,
    Harness,
    Merge,
    QuestionStop,
    Refresh,
    Release,
    StopReason,
    Wait,
    WaitReason,
    stop_tag,
)
from issue_agent.domain.checks import Check, Observed
from issue_agent.domain.evidence import ChangeRequest, CiFailure, CiFailures
from issue_agent.domain.world import BlockerRef, Comment, PRView, Push

T0 = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
HARNESS = Harness(fix_bot="fixbot", review_bot="reviewbot")
_RED = CiFailures((CiFailure("check:1@9.1", "test", 1, 9),))


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


_AUTO = object()


def status(state, *, comments=(), pr=None, legs=(), target="issue", last_end=_AUTO, now=None):
    """`last_end` is derive's item boundary; by default the newest end of `legs`."""
    if last_end is _AUTO:
        last_end = max((x.ended_at for x in legs if x.ended_at is not None), default=None)
    return dash.board_status(
        state, target=target, comments=comments, pr=pr, legs=tuple(legs), last_end=last_end, harness=HARNESS, now=now or at(600),
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
    board = status(QuestionStop("Proceed?", "-"), legs=[leg(ended=3)])
    assert (board.reason, board.detail, board.since_ts) == ("question", "Proceed?", stamp(3))
    attempts = status(AttemptsStop(AgentKind.FIX_CI, "-"))
    assert attempts.reason == "attempts" and dash.primary_action(attempts, held=True) == "unblock"
    # Policy has not labelled it yet: removing a label that is not there
    # would do nothing, so the row offers to open the item instead.
    assert dash.primary_action(attempts, held=False) == "open"


# ── working and waiting ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state,activity",
    [(Merge("h1"), "merge"), (Release(), "release"), (Wait(WaitReason.CI_PENDING), "ci")],
)
def test_moving_states_are_working(state, activity):
    board = status(state)
    assert isinstance(board, dash.Working) and board.activity == activity


@pytest.mark.parametrize("act", list(AgentKind))
@pytest.mark.parametrize("legs", [[], [leg("RESOLVE", 0, ended=2)]])
def test_every_dispatch_waits_in_the_slot_lane_whatever_the_leg_history(act, legs):
    evidence = {AgentKind.FIX_CI: _RED, AgentKind.ADDRESS_REVIEW: ChangeRequest("reviewbot", "h1", body="Handle None.")}
    board = status(Dispatch(act, "" if act in ISSUE_ACTS else "h1", evidence=evidence.get(act)), legs=legs)
    assert isinstance(board, dash.Waiting)
    # #1105: a light act (STEWARD, TRIAGE) never waits for an agent slot.
    reason = "Waiting for a free agent slot." if act.holds_dispatch_slot else (
        "Needs no agent slot; starts on the next tick unless every light slot is busy.")
    assert (board.on, board.headline, board.reason) == ("slot", f"{dash._ACT_WORDS[act.name]} next", reason)


def _reloading(state, held, **kw):
    return dash.board_status(state, target="issue", comments=(), pr=None, legs=(), last_end=None, harness=HARNESS,
                             now=at(600), reload_held=held, **kw)


@pytest.mark.parametrize(("held", "reason"), [
    ((1078,), "Restarts when #1078's leg ends."),
    ((7, 1078), "Restarts when the legs of #7, #1078 end."),
    ((), "Starts after the harness restarts, once its running light legs end."),
])
def test_heavy_work_held_by_a_reload_drain_says_so_instead_of_a_slot_wait(held, reason):
    board = _reloading(Dispatch(AgentKind.RESOLVE), {"fix": held})
    assert (board.on, board.group, board.headline, board.reason) == (
        "reload", "waiting", "Paused for a harness reload", reason)


def test_a_reload_drain_pauses_only_its_own_roles_heavy_work():
    assert _reloading(Dispatch(AgentKind.STEWARD), {"fix": (1078,)}).on == "slot"  # light legs keep starting
    assert _reloading(Dispatch(AgentKind.REVIEW, "h1"), {"fix": (1078,)}).on == "slot"
    assert _reloading(Dispatch(AgentKind.REVIEW, "h1"), {"review": (30,)}).on == "reload"
    assert _reloading(Dispatch(AgentKind.RESOLVE), {}).on == "slot"


def test_light_work_is_paused_too_once_no_heavy_leg_drains():
    # Light admission closes with the last heavy leg (no livelock), so the row must not promise "next tick".
    board = _reloading(Dispatch(AgentKind.TRIAGE), {"fix": ()})
    assert (board.on, board.reason) == ("reload", "Starts after the harness restarts, once its running light legs end.")


@pytest.mark.parametrize(("state", "age", "pending", "expected"), [
    ("running", 10.0, True, (7, 1078)),
    ("draining", 10.0, True, (7, 1078)),
    ("running", 10.0, False, None),
    ("stopped", 10.0, True, None),
    ("running", dash.STALE_AFTER_S + 1, True, None),
    ("running", -5.0, True, None),
    ("running", None, True, None),
])
def test_reload_held_trusts_only_a_live_heartbeat_and_counts_only_heavy_legs(state, age, pending, expected):
    flight = [("FIX_CI", 1078), ("TRIAGE", 1105), ("RESOLVE", 7), ("STEWARD", 1106)]
    assert dash.reload_held(state=state, age_s=age, reload_pending=pending, in_flight=flight) == expected


def test_a_queued_wait_counts_from_when_the_issue_became_ready_not_an_earlier_episode():
    # #1105 read "1d": its only legs were pre-cutover TRIAGE/GROOM rows from two days before.
    old = [leg("TRIAGE", 0, ended=3)]
    board = dash.board_status(Dispatch(AgentKind.STEWARD), target="issue", comments=(), pr=None, legs=tuple(old),
                              last_end=at(3), harness=HARNESS, now=at(600), ready_at=at(500))
    assert board.since_ts == stamp(500)
    later_end = dash.board_status(Dispatch(AgentKind.RESOLVE), target="issue", comments=(), pr=None, legs=(),
                                  last_end=at(550), harness=HARNESS, now=at(600), ready_at=at(500))
    assert later_end.since_ts == stamp(550)
    future = dash.board_status(Dispatch(AgentKind.RESOLVE), target="issue", comments=(), pr=None, legs=(),
                               last_end=None, harness=HARNESS, now=at(600), ready_at=at(700))
    assert future.since_ts is None
    unrelated = status(Dispatch(AgentKind.STEWARD), legs=old, last_end=None)
    assert unrelated.since_ts is None  # an earlier episode's leg never dates the wait


def test_a_slot_wait_is_dated_from_the_last_leg_end():
    assert status(Dispatch(AgentKind.TRIAGE), legs=[leg("TRIAGE", 0, ended=3)], last_end=at(3)).since_ts == stamp(3)
    assert status(Dispatch(AgentKind.TRIAGE), legs=[]).since_ts is None


def _ci_pr(*states):
    checks = tuple(Check(f"c{i}", state, "pending", required=True) for i, state in enumerate(states))
    return PRView(1, head_ref_oid="h", ci=Observed("h", "h", frozenset(), checks))


@pytest.mark.parametrize(
    "pr,activity,headline,reason",
    [
        (_ci_pr("QUEUED", "QUEUED"), "ci_queued", "CI queued", "2 queued, waiting for a runner."),
        (_ci_pr("IN_PROGRESS", "IN_PROGRESS", "QUEUED"), "ci", "CI running", "2 running, 1 queued"),
        (_ci_pr("IN_PROGRESS"), "ci", "CI running", "1 running"),
        (None, "ci", "CI running", "Required checks are still running."),
        (PRView(1), "ci", "CI running", "Required checks are still running."),
    ],
)
def test_ci_pending_distinguishes_queued_from_running(pr, activity, headline, reason):
    board = status(Wait(WaitReason.CI_PENDING), pr=pr)
    assert (board.activity, board.headline, board.reason) == (activity, headline, reason)


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


def _ci_pr_at(checks=(), pushes=()):
    return PRView(1, head_ref_oid="h", pushes=tuple(Push("bot", p) for p in pushes),
                  ci=Observed("h", "h", frozenset(), tuple(Check(f"c{i}", "IN_PROGRESS", "pending", started_at=s)
                                                           for i, s in enumerate(checks))))


def _ci_since(pr, *, legs=(), last_end=None, now=None):
    board = status(Wait(WaitReason.CI_PENDING), pr=pr, legs=legs, last_end=last_end, now=now)
    assert isinstance(board, dash.Working) and board.activity == "ci"
    return board.since_ts


def test_ci_step_starts_at_the_earliest_check_start():
    pr = _ci_pr_at(checks=(stamp(9), stamp(7), ""), pushes=(stamp(5),))
    assert _ci_since(pr, last_end=at(3)) == stamp(7)


def test_ci_step_without_check_starts_falls_back_to_the_newest_push_then_last_end():
    assert _ci_since(_ci_pr_at(pushes=(stamp(4), stamp(6))), last_end=at(3)) == stamp(6)
    assert _ci_since(_ci_pr_at(), last_end=at(3)) == stamp(3)


def test_a_pr_row_in_ci_with_no_pr_keyed_leg_still_has_a_step_start():
    # Live bug: the only leg (RESOLVE) is keyed to the issue, so derive's last_end is None.
    board = status(Wait(WaitReason.CI_PENDING), pr=_ci_pr_at(), legs=[leg("RESOLVE", 2)], last_end=None, target="pr")
    assert isinstance(board, dash.Working) and board.since_ts == stamp(3)


@pytest.mark.parametrize("bad", ["garbage", "", "2026-13-45T99:00:00Z"])
def test_malformed_ci_timestamps_are_absent(bad):
    pr = _ci_pr_at(checks=(bad,), pushes=(bad,))
    assert _ci_since(pr, last_end=at(3)) == stamp(3)


def test_future_ci_timestamps_are_absent_not_clamped():
    pr = _ci_pr_at(checks=(stamp(700),), pushes=(stamp(8), stamp(800)))
    assert _ci_since(pr, last_end=at(3), now=at(10)) == stamp(8)
    assert _ci_since(_ci_pr_at(checks=(stamp(700),)), last_end=at(3), now=at(10)) == stamp(3)


def test_checks_of_another_head_do_not_date_the_step():
    pr = PRView(1, head_ref_oid="new", ci=Observed("old", "old", frozenset(), (Check("c", "IN_PROGRESS", started_at=stamp(1)),)))
    assert _ci_since(pr, last_end=at(3)) == stamp(3)


def test_free_action_steps_start_when_the_previous_step_ended_falling_back_to_issue_legs():
    legs = [leg("RESOLVE", 2), leg("REVIEW", 5)]
    for state in (Merge("h"), Refresh("h"), Release()):
        assert status(state, legs=legs, last_end=None).since_ts == stamp(6)
    assert status(Merge("h"), legs=legs, last_end=at(1)).since_ts == stamp(1)
    assert status(Merge("h"), last_end=None).since_ts is None


# ── rail and history ─────────────────────────────────────────────────────────


def test_the_rail_counts_legs_and_remembers_a_rewind():
    legs = (
        leg("RESOLVE", 0),
        leg("REVIEW", 2, verdict="REQUEST_CHANGES", number=9),
        leg("FIX_CI", 4, number=9),
        leg("REBASE", 6, number=9),
        leg("RESOLVE", 8, state="released"),
    )
    rail = dash.rail(Dispatch(AgentKind.FIX_CI, "h1", evidence=_RED), has_pr=True, legs=legs)
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


# ── board groups ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "board,group",
    [
        (dash.Working("agent", "RESOLVE", "h", "r", None), "agent"),
        (dash.Working("agent", "TRIAGE", "h", "r", None), "agent"),
        (dash.Working("ci", None, "h", "r", None), "ci"),
        (dash.Working("ci_queued", None, "h", "r", None), "ci"),
        *((dash.Working(activity, None, "h", "r", None), "ci")
          for activity in ("merge", "update", "open_pr", "release", "triage_note", "blocker_link")),
        (dash.Waiting("slot", "h", "r", None), "slot"),
        *((dash.Waiting(on, "h", "r", None), "waiting")
          for on in ("branch-protection", "human-review", "headroom", "backoff", "blocked", "github")),
        (dash.NeedsYou("question", "h", None, "issue", None), "needs_you"),
    ],
)
def test_every_board_status_lands_in_one_group(board, group):
    assert board.group == group


@pytest.mark.parametrize(
    "state,group",
    [
        (Dispatch(AgentKind.FIX_CI, "h1", evidence=_RED), "slot"),
        (Dispatch(AgentKind.RESOLVE), "slot"),
        (Wait(WaitReason.CI_PENDING), "ci"),
        (Merge("h1"), "ci"),
        (Refresh("h1"), "ci"),
        (Wait(WaitReason.HEADROOM), "waiting"),
        (Backoff(AgentKind.RESOLVE, at(30)), "waiting"),
        (Blocked((BlockerRef(9),)), "waiting"),
        (QuestionStop("q", "-"), "needs_you"),
    ],
)
def test_derived_states_map_to_their_group(state, group):
    assert status(state).group == group


def test_slot_usage_counts_heavy_legs_against_capacity_and_light_ones_beside_it():
    agent = lambda act: dash.Working("agent", act, "h", "r", None)  # noqa: E731
    boards = [agent("RESOLVE"), agent("REVIEW"), agent("TRIAGE"), agent("STEWARD"),
              dash.Working("ci", None, "h", "r", None), dash.Waiting("slot", "h", "r", None)]
    assert dash.slot_usage(boards, 3) == dash.SlotUsage(capacity=3, heavy=2, light=2)
    assert dash.slot_usage([], 3) == dash.SlotUsage(3, 0, 0)


# ── blocked by ───────────────────────────────────────────────────────────────


def test_a_blocked_wait_is_headlined_by_its_blocker_in_the_waiting_lane():
    board = status(Blocked((BlockerRef(9),)))
    assert isinstance(board, dash.Waiting)
    assert (board.group, board.on, board.headline, board.blocker) == ("waiting", "blocked", "Blocked by #9", BlockerRef(9))
    assert "starts by itself" in board.reason


def test_several_blockers_headline_the_lowest_own_repository_one_and_count_the_rest():
    board = status(Blocked((BlockerRef(2, "x/lib"), BlockerRef(12), BlockerRef(5))))
    assert isinstance(board, dash.Waiting)
    assert (board.headline, board.blocker) == ("Blocked by #5", BlockerRef(5))
    assert "2 more" in board.reason


def test_a_cross_repository_blocker_is_named_with_its_repository():
    board = status(Blocked((BlockerRef(2, "x/lib"),)))
    assert isinstance(board, dash.Waiting) and board.headline == "Blocked by x/lib#2"


def test_a_huge_blocker_list_keeps_a_one_line_headline():
    board = status(Blocked(tuple(BlockerRef(n) for n in range(10_000, 0, -1))))
    assert isinstance(board, dash.Waiting) and board.headline == "Blocked by #1" and "9999 more" in board.reason


def test_a_blocked_issue_rails_at_triage_before_its_first_act_and_at_its_last_stage_after():
    blocked = Blocked((BlockerRef(9),))
    assert dash.rail(blocked, has_pr=False, legs=()).current == "triage"
    assert dash.rail(blocked, has_pr=False, legs=(leg("RESOLVE", 0),)).current == "resolve"



def test_a_pending_blocker_link_is_ci_lane_work_at_the_triage_stage():
    from issue_agent.domain.state import LinkBlocker

    link = LinkBlocker(4, "Needs #4", "2026-10-13T07:00:00+00:00")
    board = status(link)
    assert isinstance(board, dash.Working)
    assert (board.activity, board.group, board.headline) == ("blocker_link", "ci", "Linking blocker")
    assert "#4" in board.reason
    assert dash.rail(link, has_pr=False, legs=()).current == "triage"
