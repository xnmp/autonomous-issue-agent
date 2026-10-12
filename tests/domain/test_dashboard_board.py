"""The issues board's pure folds: the stage-visit track and the status union.

Assertions are on what the board shows: visit order, the current and furthest
stage, the counts, and which group a row lands in with what headline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain import dashboard as dash
from issue_agent.domain.dashboard import (
    Ask,
    BoardFacts,
    OnIssue,
    OnOverlap,
    OnPr,
    OnQueue,
    OnTriage,
    Stage,
    StepOutcome as O,
    TrackStep,
    stage_track,
)

T0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def steps(*specs) -> list[TrackStep]:
    """(kind, outcome[, verdict]) in time order, one minute apart."""
    out = []
    for index, spec in enumerate(specs):
        kind, outcome, *rest = spec
        out.append(TrackStep(kind, outcome, at(index), verdict=bool(rest and rest[0])))
    return out


def visit_shape(track) -> list[tuple[str, list[tuple[str, str, int]]]]:
    return [
        (visit.stage.value, [(chip.kind, chip.outcome.value, chip.count) for chip in visit.chips])
        for visit in track.visits
    ]


# ── stage track ───────────────────────────────────────────────────────────────
def test_an_issue_with_no_history_sits_at_triage_with_nothing_counted():
    track = stage_track([])

    assert track.visits == ()
    assert (track.current, track.furthest) == (Stage.TRIAGE, Stage.TRIAGE)
    assert (track.resolve_attempts, track.review_rounds, track.ci_runs, track.steps) == (0, 0, 0, 0)


def test_resolve_only_history_is_one_resolve_visit():
    track = stage_track(steps(("ROUTE", O.DONE), ("RESOLVE", O.FAILED), ("RESOLVE", O.RUNNING)))

    assert visit_shape(track) == [
        ("resolve", [("ROUTE", "done", 1), ("RESOLVE", "failed", 1), ("RESOLVE", "running", 1)]),
    ]
    assert track.current is Stage.RESOLVE
    assert track.furthest is Stage.RESOLVE
    assert track.resolve_attempts == 2


def test_review_and_ci_rewinds_start_new_visits_in_order():
    track = stage_track(steps(
        ("RESOLVE", O.DONE),
        ("REVIEW", O.PASSED, True),
        ("CI", O.FAILED),
        ("FIX_CI", O.DONE),
        ("RE_REVIEW", O.CHANGES_REQUESTED, True),
        ("ADDRESS_REVIEW", O.DONE),
        ("RE_REVIEW", O.PASSED, True),
        ("CI", O.RUNNING),
    ))

    assert [visit.stage for visit in track.visits] == [
        Stage.RESOLVE, Stage.REVIEW, Stage.CI, Stage.REVIEW, Stage.CI,
    ]
    assert track.current is Stage.CI
    assert track.furthest is Stage.CI
    assert (track.review_rounds, track.ci_runs) == (3, 2)


def test_a_rewind_leaves_the_furthest_stage_ahead_of_the_current_one():
    track = stage_track(steps(
        ("RESOLVE", O.DONE), ("REVIEW", O.PASSED, True), ("CI", O.FAILED),
        ("FIX_CI", O.DONE), ("RE_REVIEW", O.RUNNING),
    ))

    assert track.current is Stage.REVIEW
    assert track.furthest is Stage.CI


def test_checks_before_an_approval_stay_in_the_stage_in_progress():
    track = stage_track(steps(
        ("RESOLVE", O.DONE), ("CI", O.PASSED), ("REVIEW", O.CHANGES_REQUESTED, True),
        ("ADDRESS_REVIEW", O.DONE), ("CI", O.FAILED),
    ))

    assert visit_shape(track) == [
        ("resolve", [("RESOLVE", "done", 1), ("CI", "passed", 1)]),
        ("review", [("REVIEW", "changes_requested", 1), ("ADDRESS_REVIEW", "done", 1), ("CI", "failed", 1)]),
    ]
    assert (track.current, track.furthest) == (Stage.REVIEW, Stage.REVIEW)
    assert track.ci_runs == 2


def test_an_approval_moves_the_issue_on_to_ci_before_any_check_reports():
    track = stage_track(steps(("RESOLVE", O.DONE), ("REVIEW", O.PASSED, True)))

    assert [visit.stage for visit in track.visits] == [Stage.RESOLVE, Stage.REVIEW]
    assert (track.current, track.furthest) == (Stage.CI, Stage.CI)


def test_a_passing_review_run_that_decided_nothing_does_not_move_on():
    track = stage_track(steps(("RESOLVE", O.DONE), ("REVIEW", O.PASSED)))

    assert track.current is Stage.REVIEW


def test_a_ci_fix_before_any_approval_is_no_rewind_from_ci():
    # PR opens, CI fails, the fixer repairs it, then review asks for changes:
    # the patch never reached the CI stage, so nothing was sent back from it.
    track = stage_track(steps(
        ("RESOLVE", O.DONE), ("CI", O.FAILED), ("FIX_CI", O.DONE),
        ("REVIEW", O.CHANGES_REQUESTED, True), ("FIX_CI", O.RUNNING),
    ))

    assert visit_shape(track) == [
        ("resolve", [("RESOLVE", "done", 1), ("CI", "failed", 1), ("FIX_CI", "done", 1)]),
        ("review", [("REVIEW", "changes_requested", 1), ("FIX_CI", "running", 1)]),
    ]
    assert (track.current, track.furthest) == (Stage.REVIEW, Stage.REVIEW)
    assert track.ci_runs == 1


@pytest.mark.parametrize("decision,stage", [(O.CHANGES_REQUESTED, Stage.REVIEW), (O.PASSED, Stage.CI)])
def test_a_rebase_is_a_repair_inside_the_stage_in_progress(decision, stage):
    track = stage_track(steps(
        ("RESOLVE", O.DONE), ("REVIEW", decision, True), ("FIX_CI", O.DONE),
        ("REBASE", O.DONE), ("PR_RESUME", O.DONE),
    ))

    assert [visit.stage for visit in track.visits][-1] is stage
    assert [chip.kind for chip in track.visits[-1].chips][-2:] == ["REBASE", "PR_RESUME"]
    assert track.current is stage


def test_a_rebase_counts_a_ci_run_only_through_the_head_ci_ran_on():
    track = stage_track(steps(
        ("RESOLVE", O.DONE), ("REVIEW", O.PASSED, True), ("CI", O.PASSED),
        ("REBASE", O.DONE), ("CI", O.RUNNING),
    ))

    assert track.ci_runs == 2
    assert track.current is Stage.CI


def test_consecutive_identical_steps_fold_into_one_chip_with_a_count():
    track = stage_track(steps(
        ("RESOLVE", O.FAILED), ("RESOLVE", O.FAILED), ("RESOLVE", O.FAILED),
        ("RESOLVE", O.DONE),
    ))

    assert visit_shape(track) == [("resolve", [("RESOLVE", "failed", 3), ("RESOLVE", "done", 1)])]
    assert track.steps == 4
    assert track.resolve_attempts == 4


def test_an_unknown_kind_inherits_the_stage_in_progress():
    track = stage_track(steps(("RESOLVE", O.DONE), ("REVIEW", O.CHANGES_REQUESTED, True), ("SOMETHING_NEW", O.DONE)))

    assert visit_shape(track)[-1] == (
        "review", [("REVIEW", "changes_requested", 1), ("SOMETHING_NEW", "done", 1)],
    )


def test_an_unknown_kind_first_inherits_triage():
    track = stage_track(steps(("unblock", O.DONE)))

    assert track.current is Stage.TRIAGE
    assert visit_shape(track) == [("triage", [("unblock", "done", 1)])]


def test_steps_are_ordered_by_time_and_unstamped_steps_sort_last_in_their_order():
    track = stage_track([
        TrackStep("FIX_CI", O.DONE, None),
        TrackStep("REVIEW", O.PASSED, at(5), verdict=True),
        TrackStep("RESOLVE", O.DONE, at(1)),
        TrackStep("unblock", O.DONE, None),
    ])

    assert visit_shape(track) == [
        ("resolve", [("RESOLVE", "done", 1)]),
        ("review", [("REVIEW", "passed", 1)]),
        ("ci", [("FIX_CI", "done", 1), ("unblock", "done", 1)]),
    ]


def test_naive_and_aware_timestamps_order_together_without_raising():
    track = stage_track([
        TrackStep("REVIEW", O.PASSED, at(5), verdict=True),
        TrackStep("RESOLVE", O.DONE, at(1).replace(tzinfo=None)),
    ])

    assert [visit.stage for visit in track.visits] == [Stage.RESOLVE, Stage.REVIEW]


def test_a_journal_without_head_observations_counts_its_ci_fixes_as_runs():
    track = stage_track(steps(("RESOLVE", O.DONE), ("FIX_CI", O.DONE), ("FIX_CI", O.FAILED)))

    assert track.ci_runs == 2


def test_failed_review_runs_decide_nothing_and_count_no_round():
    track = stage_track(steps(("RESOLVE", O.DONE), ("REVIEW", O.FAILED), ("REVIEW", O.PASSED, True)))

    assert track.review_rounds == 1


def test_a_very_long_history_folds_in_order_with_exact_counts():
    # Approved, then CI fails and its fix sends the patch back to review.
    cycle = [
        ("REVIEW", O.PASSED, True),
        ("CI", O.FAILED),
        ("FIX_CI", O.DONE),
    ]
    history = [("RESOLVE", O.DONE)] + cycle * 150 + [("MERGE", O.DONE)]

    track = stage_track(steps(*history))

    assert track.steps == 452
    assert (track.review_rounds, track.ci_runs, track.resolve_attempts) == (150, 150, 1)
    assert len(track.visits) == 1 + 2 * 150 + 1
    assert track.current is Stage.MERGE
    assert track.furthest is Stage.MERGE


# ── status union ──────────────────────────────────────────────────────────────
EMPTY = stage_track([])
REVIEWED = stage_track(steps(("RESOLVE", O.DONE), ("REVIEW", O.PASSED, True)))


def test_a_breaker_escalation_asks_for_a_re_arm_without_its_framing():
    status = dash.board_status(BoardFacts(
        track=EMPTY,
        attention=dash.Escalated(
            at(0),
            "Circuit breaker: resolve failed 3x without opening a PR. Escalating to a human.",
            breaker=True,
        ),
    ))

    assert isinstance(status, dash.NeedsYou)
    assert status.ask is Ask.REARM
    assert status.headline == "Re-arm or close"
    assert status.reason == "Resolve failed 3x without opening a PR."
    assert status.since == at(0)


def test_budget_review_outranks_the_breaker_reading():
    status = dash.board_status(BoardFacts(
        track=EMPTY,
        attention=dash.Escalated(at(0), "Budget review needed: the resolve leg reached its cap", breaker=False, budget=True),
    ))

    assert isinstance(status, dash.NeedsYou) and status.ask is Ask.BUDGET
    assert status.reason == "The resolve leg reached its cap."


def test_an_escalation_without_a_recorded_reason_still_says_what_to_do():
    status = dash.board_status(BoardFacts(track=EMPTY, attention=dash.Escalated(None)))

    assert isinstance(status, dash.NeedsYou) and status.ask is Ask.ESCALATED
    assert status.reason == "Escalated. See the issue comments."


def test_awaiting_a_reply_carries_the_agents_question():
    status = dash.board_status(BoardFacts(track=EMPTY, attention=dash.AwaitingReply(at(3), "which config wins?")))

    assert isinstance(status, dash.NeedsYou) and status.ask is Ask.AWAITING_REPLY
    assert status.reason == "Which config wins?"


def test_escalated_while_a_leg_is_running_stays_needs_you():
    status = dash.board_status(BoardFacts(
        track=EMPTY, attention=dash.Escalated(at(0)), running_kind="FIX_CI",
    ))

    assert isinstance(status, dash.NeedsYou)


@pytest.mark.parametrize(
    "owner,group,says",
    [
        ("harness", dash.Working, "The harness merges it next."),
        ("github", dash.Working, "GitHub merges it automatically."),
        ("operator", dash.NeedsYou, "The harness does not merge this PR."),
    ],
)
def test_an_approved_green_pr_goes_to_whoever_owns_its_merge(owner, group, says):
    status = dash.board_status(BoardFacts(
        track=REVIEWED, ci="passing", merge_owner=owner, last_activity=at(3),
    ))

    assert isinstance(status, group)
    assert says in status.reason
    assert status.since == at(3)
    if isinstance(status, dash.NeedsYou):
        assert (status.ask, status.headline) == (Ask.MERGE, "Merge the PR")
    else:
        assert status.headline == "Ready to merge"


@pytest.mark.parametrize("owner", ["harness", "operator"])
def test_a_merge_github_refuses_is_nobodys_merge_yet(owner):
    status = dash.board_status(BoardFacts(
        track=REVIEWED, ci="passing", merge_owner=owner, merge_refused=True,
    ))

    assert isinstance(status, dash.Working)
    assert (status.headline, status.activity) == ("Approved", dash.Activity.HANDOFF)
    assert "GitHub does not allow the merge yet" in status.reason


@pytest.mark.parametrize(
    "after",
    [("FIX_CI", O.DONE), ("ADDRESS_REVIEW", O.DONE), ("REBASE", O.DONE), ("PR_RESUME", O.DONE), ("RESUME", O.DONE)],
)
def test_an_approval_does_not_survive_a_changed_patch(after):
    track = stage_track(steps(("RESOLVE", O.DONE), ("REVIEW", O.PASSED, True), after))

    status = dash.board_status(BoardFacts(track=track, ci="passing", merge_owner="harness"))

    assert isinstance(status, dash.Working)
    assert status.activity is dash.Activity.HANDOFF
    assert status.headline not in {"Ready to merge", "Approved"}


def test_github_requiring_a_review_on_the_current_head_overrides_the_journal_approval():
    status = dash.board_status(BoardFacts(
        track=REVIEWED, ci="passing", merge_owner="harness", github_approved=False,
    ))

    assert isinstance(status, dash.Working) and status.headline != "Ready to merge"


def test_an_unread_github_review_decision_trusts_the_current_journal_approval():
    status = dash.board_status(BoardFacts(track=REVIEWED, ci="passing", github_approved=None))

    assert isinstance(status, dash.NeedsYou) and status.ask is Ask.MERGE


def test_parked_outranks_an_escalation_and_reads_as_a_red_triage_wait():
    status = dash.board_status(BoardFacts(
        track=EMPTY,
        attention=dash.Escalated(at(0)),
        triage=dash.TriageHold("parked", at(1)),
    ))

    assert isinstance(status, dash.Blocked)
    assert status.on == OnTriage("parked")
    assert status.headline == "Waiting on triage"


def test_a_steward_question_is_blocked_on_triage_below_holds():
    question = dash.TriageHold("awaiting_reply", at(1))

    assert dash.board_status(BoardFacts(track=EMPTY, triage=question)).on == OnTriage("awaiting_reply")  # type: ignore[union-attr]
    held = dash.board_status(BoardFacts(
        track=EMPTY, triage=question, hold=dash.Hold(OnIssue(1082), at(0)),
    ))
    assert isinstance(held, dash.Blocked) and held.on == OnIssue(1082)


@pytest.mark.parametrize(
    "on,headline",
    [
        (OnIssue(1082), "Waiting for #1082"),
        (OnIssue(None), "Waiting on a dependency"),
        (OnPr(77), "Waiting for PR #77"),
        (OnOverlap(issue=12), "Waiting for #12"),
        (OnOverlap(pr=88), "Waiting for PR #88"),
        (OnOverlap(), "Waiting on an overlap"),
    ],
)
def test_a_hold_is_blocked_on_its_target(on, headline):
    status = dash.board_status(BoardFacts(track=EMPTY, hold=dash.Hold(on, at(4))))

    assert isinstance(status, dash.Blocked)
    assert (status.on, status.headline, status.since) == (on, headline, at(4))


def test_a_running_leg_outranks_a_hold_and_says_what_it_does():
    status = dash.board_status(BoardFacts(
        track=REVIEWED, running_kind="RE_REVIEW", hold=dash.Hold(OnIssue(5), at(0)), last_activity=at(9),
    ))

    assert isinstance(status, dash.Working)
    assert (status.headline, status.reason, status.since) == ("Reviewing", "Review round 2.", at(9))


def test_an_unknown_running_kind_still_reads_as_working():
    status = dash.board_status(BoardFacts(track=EMPTY, running_kind="MYSTERY"))

    assert isinstance(status, dash.Working) and status.headline == "Agent running"


def test_ci_running_on_an_approved_head_names_the_approving_round():
    status = dash.board_status(BoardFacts(track=REVIEWED, ci="pending"))

    assert isinstance(status, dash.Working)
    assert (status.headline, status.reason) == ("CI running", "Approved on review round 1.")


def test_a_stall_is_needs_you_in_the_harness_stopped_wording_with_its_deferral():
    status = dash.board_status(BoardFacts(
        track=REVIEWED, stall=dash.StallObserved(at(0), "fix_leg_active"),
    ))

    assert isinstance(status, dash.NeedsYou) and status.ask is Ask.STALLED
    assert status.reason == "Deferred: fix_leg_active. The harness stopped without escalating."
    assert status.since == at(0)


@pytest.mark.parametrize(
    "facts",
    [
        {"running_kind": "RESOLVE"},
        {"hold": dash.Hold(OnIssue(3), None)},
        {"attention": dash.Escalated(None)},
        {"ci": "pending"},
    ],
)
def test_a_stall_never_shadows_a_leg_a_hold_an_escalation_or_running_ci(facts):
    status = dash.board_status(BoardFacts(track=REVIEWED, stall=dash.StallObserved(at(0)), **facts))

    assert not (isinstance(status, dash.NeedsYou) and status.ask is Ask.STALLED)


def test_a_queued_issue_is_blocked_on_the_queue_with_no_wait_start():
    status = dash.board_status(BoardFacts(track=EMPTY, queued=True, priority="P2"))

    assert isinstance(status, dash.Blocked)
    assert (status.on, status.since) == (OnQueue("P2"), None)
    assert "(P2)" in status.reason


def test_with_nothing_running_or_held_the_harness_owns_the_next_move():
    changes = stage_track(steps(("RESOLVE", O.DONE), ("REVIEW", O.CHANGES_REQUESTED, True)))
    status = dash.board_status(BoardFacts(track=changes))

    assert isinstance(status, dash.Working)
    assert status.activity is dash.Activity.HANDOFF
    assert status.headline == "Changes requested"


def test_every_combination_of_facts_yields_exactly_one_group():
    """Contradictory inputs (escalated and parked and held and stalled and
    queued and merge-ready) must still produce one coherent state, never an
    error."""
    attentions = [None, dash.Escalated(None, breaker=True, budget=True), dash.AwaitingReply(None)]
    triages = [None, dash.TriageHold("parked", None), dash.TriageHold("awaiting_reply", None)]
    holds = [None, dash.Hold(OnPr(1), None)]
    for attention in attentions:
        for triage in triages:
            for hold in holds:
                for running in (None, "REVIEW"):
                    for stall in (None, dash.StallObserved(None)):
                        for ci in ("pending", "passing"):
                            status = dash.board_status(BoardFacts(
                                track=REVIEWED, attention=attention, triage=triage, hold=hold,
                                running_kind=running, stall=stall, queued=True, ci=ci,
                                github_approved=True,
                            ))
                            assert isinstance(status, (dash.NeedsYou, dash.Working, dash.Blocked))
                            assert status.headline and status.reason


# ── overspend ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "cost,budget,ratio",
    [
        (1.99, 1.0, None),
        (2.0, 1.0, 2.0),
        (33.35, 4.28, 7.79),
        (5.0, 0.0, None),
        (5.0, -1.0, None),
        (None, 4.0, None),
        (5.0, None, None),
        (float("nan"), 1.0, None),
        (float("inf"), 1.0, None),
        (True, 0.5, None),
        ("9", 1.0, None),
        (1e300, 1e-300, None),
        (10**400, 1.0, None),
        (5, 10**400, None),
    ],
)
def test_overspend_alarms_only_at_twice_the_projection(cost, budget, ratio):
    assert dash.overspend_ratio(cost, budget) == ratio
