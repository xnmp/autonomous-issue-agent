"""A repair carries its evidence, only new failure evidence dispatches, a
resume within one try (a push or a re-run) is no attempt, the harness re-runs
a flake once per head, and derive reads its own writes instead of waiting on a
timer (tauri #1078; owner, PR #403)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.backoff import LegEnd
from issue_agent.domain.checks import Check, Observed, RequiredCheck
from issue_agent.domain.evidence import (
    MAX_FAILURES, ChangeRequest, CiFailure, CiFailures, began, new_failures, recorded_keys,
)
from issue_agent.domain.routing import DEFAULT_TIER, Tier, escalate
from issue_agent.domain.state import (
    Backoff, Dispatch, Harness, Local, NeedsHuman, Rerun, StopReason, Wait, WaitReason, derive, pr_key,
)
from issue_agent.domain.world import Comment, PRView, Review, WorldState

T0 = datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc)
H = Harness("fix-bot", "review-bot")
UBUNTU, WINDOWS = "smoke (ubuntu-latest)", "smoke (windows-latest)"
POLICY = frozenset({RequiredCheck(UBUNTU)})


def at(minute: float) -> datetime:
    return T0 + timedelta(minutes=minute)


# When a run began: the push's runs before any leg; a re-run after every leg below began.
PUSHED, AFTER = "2026-10-14T00:50:00Z", at(45).isoformat()


def job(name: str, check_id: int, bucket: str, *, required: bool = False, run: int = 900,
        attempt: int = 1, start: str = "") -> Check:
    state = {"fail": "FAILURE", "pass": "SUCCESS", "pending": "IN_PROGRESS"}[bucket]
    return Check(name, state, bucket, start or (PUSHED if attempt == 1 else AFTER), run_id=run, required=required,
                 check_id=check_id, run_attempt=attempt)


def k(check_id: int, run: int = 900, attempt: int = 1, start: str = "") -> str:
    return f"check:{check_id}@{start or (PUSHED if attempt == 1 else AFTER)}"


def ci(*checks: Check, head: str = "h1") -> Observed:
    return Observed(head, head, POLICY, checks)


def pr(observed: Observed, head: str = "h1", **kw) -> PRView:
    return PRView(number=7, head_ref_name="fix/3-x", head_ref_oid=head, ci=observed, mergeable="MERGEABLE",
                  merge_state="CLEAN", **kw)


def fix_end(minute: float, *keys: str, head: str = "h1", machine: bool = False, tier: Tier | None = None,
            end_head: str = "", capped: bool = False) -> LegEnd:
    return LegEnd(AgentKind.FIX_CI, at(minute), machine, head, started_at=at(minute - 1),
                  evidence=frozenset(keys), tier=tier, end_head=end_head, capped=capped)


def state(view: PRView, *ends: LegEnd, now: float = 60):
    world = WorldState(prs=(view,), now=at(now).isoformat())
    return derive(world, Local(now=at(now), legs={pr_key(7): ends}), H)[pr_key(7)]


def stop(minute: float) -> NeedsHuman:
    return NeedsHuman(StopReason.ATTEMPTS, at(minute).isoformat(), AgentKind.FIX_CI)


# ── the #1078 run, end to end in derive ─────────────────────────────────────

FIRST = ci(job(UBUNTU, 11, "fail", required=True), job(WINDOWS, 12, "pending"))
SECOND = ci(job(UBUNTU, 11, "fail", required=True), job(WINDOWS, 12, "fail"))
DONE = ci(job(UBUNTU, 11, "fail", required=True), job(WINDOWS, 12, "pass"))
# The harness's re-run of DONE: attempt 2 mints new job ids.
RERUN = ci(job(UBUNTU, 21, "fail", required=True, attempt=2), job(WINDOWS, 22, "pass", attempt=2))


def test_the_first_failure_dispatches_while_a_sibling_job_runs_with_where_to_read_its_log():
    step = state(pr(FIRST))
    assert isinstance(step, Dispatch) and step.act is AgentKind.FIX_CI
    assert step.evidence == CiFailures((CiFailure(k(11), UBUNTU, 11, 900),), running=(WINDOWS,))


def test_a_sibling_failing_after_the_leg_ended_dispatches_just_the_new_failure():
    step = state(pr(SECOND), fix_end(30, k(11)))
    assert isinstance(step, Dispatch)
    assert step.evidence == CiFailures((CiFailure(k(12), WINDOWS, 12, 900),), handled=(UBUNTU,))


def test_the_same_evidence_never_dispatches_again():
    assert state(pr(FIRST), fix_end(30, k(11))) == Wait(WaitReason.CI_PENDING)  # the sibling still runs
    both = (fix_end(30, k(11)), fix_end(40, k(12)))
    assert not isinstance(state(pr(SECOND), *both), Dispatch)


# ── the harness re-runs a flake once per head ───────────────────────────────


def test_a_flake_the_leg_left_while_a_sibling_ran_ends_in_a_harness_rerun_not_a_stop():
    # The owner's bug: the agent judged ubuntu a flake while windows still ran.
    end = fix_end(30, k(11))
    assert state(pr(FIRST), end) == Wait(WaitReason.CI_PENDING)
    assert state(pr(DONE), end) == Rerun("h1", ((900, 1),))


def test_a_failure_after_the_harness_rerun_goes_to_the_agent_as_a_counted_attempt():
    step = state(pr(RERUN), fix_end(30, k(11), tier=DEFAULT_TIER))
    assert isinstance(step, Dispatch) and step.evidence.keys == (k(21, attempt=2),)
    assert step.tier == escalate(DEFAULT_TIER, 1)


def test_a_head_already_rerun_stops_for_a_human():
    legs = (fix_end(30, k(11)), fix_end(40, k(21, attempt=2)))
    assert state(pr(RERUN), *legs) == stop(40)


def test_a_failure_with_no_actions_run_to_rerun_stops_for_a_human():
    status = Check("sonar", "FAILURE", "fail", started_at="2026-10-14T00:50:00Z", required=True, kind="status")
    observed = Observed("h1", "h1", frozenset({RequiredCheck("sonar")}), (status,))
    fresh = new_failures(observed, ())
    assert fresh is not None
    assert state(pr(observed), fix_end(30, *fresh.keys)) == stop(30)


def test_every_failed_run_of_the_head_is_rerun_with_its_observed_attempt():
    two = ci(job(UBUNTU, 11, "fail", required=True), job("lint", 31, "fail", run=901), job(WINDOWS, 12, "pass"))
    assert state(pr(two), fix_end(30, k(11), k(31, run=901))) == Rerun("h1", ((900, 1), (901, 1)))


# ── read-your-writes, not a timer (owner, PR #403) ──────────────────────────


def test_a_push_github_does_not_show_yet_waits_then_flows_normally_once_shown():
    pushed = fix_end(30, k(11), end_head="h2")  # the harness read h2 on the remote after the leg
    assert state(pr(DONE), pushed, now=90) == Wait(WaitReason.CI_UNKNOWN)  # however long it lags
    shown = ci(job(UBUNTU, 41, "fail", required=True), head="h2")
    step = state(pr(shown, head="h2"), pushed, now=30.1)
    assert isinstance(step, Dispatch) and step.head == "h2" and step.evidence.keys == (k(41),)


def test_a_leg_that_pushed_nothing_is_decided_on_at_once():
    for end_head in ("h1", ""):  # the remote still at the decided head, or not read
        assert state(pr(DONE), fix_end(30, k(11), end_head=end_head), now=30.01) == Rerun("h1", ((900, 1),))


def test_a_head_moved_past_the_legs_push_is_not_waited_on():
    # update-branch or a human moved the PR on from the leg's push: GitHub's head is current.
    moved = ci(job(UBUNTU, 51, "fail", required=True), head="h3")
    step = state(pr(moved, head="h3"), fix_end(30, k(11), end_head="h2"))
    assert isinstance(step, Dispatch) and step.head == "h3"


def test_a_human_reset_after_the_push_ends_the_wait():
    # A human reset the branch back to the decided head and said so: no forever-wait on a push GitHub never shows.
    reset = pr(DONE, comments=(Comment("alice", "reverted the bot's push; try again", at(45).isoformat()),))
    step = state(reset, fix_end(30, k(11), end_head="h2"))
    assert isinstance(step, Dispatch) and step.head == "h1" and step.evidence.keys == (k(11),)


def test_a_comment_inside_or_before_the_pushing_leg_does_not_end_the_wait():
    for minute in (29.5, 30):  # the agent's own comment inside its window, or one at its end
        talk = pr(DONE, comments=(Comment("fix-bot", "pushed a fix", at(minute).isoformat()),))
        assert state(talk, fix_end(30, k(11), end_head="h2")) == Wait(WaitReason.CI_UNKNOWN)


def test_only_the_newest_agent_legs_push_holds_the_observation():
    older_push = fix_end(20, k(1), head="h0", end_head="h1")
    assert state(pr(DONE), older_push, fix_end(30, k(11), end_head="h1")) == Rerun("h1", ((900, 1),))
    died = fix_end(40, head="h1", machine=True)  # a machine end after the push records no head
    assert state(pr(DONE), fix_end(30, k(11), end_head="h2"), died) == Wait(WaitReason.CI_UNKNOWN)


def test_a_rerun_accepted_with_no_jobs_yet_waits_instead_of_stopping():
    # The observation reads a superseded check with no current twin as pending.
    queued = Check(UBUNTU, "QUEUED", "pending", run_id=900, required=True, run_attempt=2)
    accepted = ci(queued, job(WINDOWS, 22, "pass", attempt=2))
    assert state(pr(accepted), fix_end(30, k(11))) == Wait(WaitReason.CI_PENDING)


def test_a_partial_rerun_hands_its_copied_failure_as_the_reruns_attempt():
    # A human re-ran only job 12; GitHub copied failing job 11 into attempt 2 as job 23.
    partial = ci(job(UBUNTU, 23, "fail", required=True, attempt=2), job(WINDOWS, 22, "pass", attempt=2))
    step = state(pr(partial), fix_end(30, k(11), k(12)))
    assert isinstance(step, Dispatch) and step.evidence.keys == (k(23, attempt=2),)
    assert step.tier == escalate(DEFAULT_TIER, 1)


def test_a_failure_without_a_known_run_attempt_is_never_rerun():
    unknown = Check(UBUNTU, "FAILURE", "fail", run_id=900, required=True, check_id=11)  # attempt unread
    assert state(pr(ci(unknown)), fix_end(30, "check:11@")) == stop(30)


def test_only_a_check_of_a_failing_run_holds_the_rerun():
    stuck = Check("deploy", "WAITING", "pending", run_id=950, check_id=99, run_attempt=1)  # awaits an approval
    assert state(pr(ci(*DONE.reports, stuck)), fix_end(30, k(11))) == Rerun("h1", ((900, 1),))
    sibling = job(WINDOWS, 12, "pending")
    assert state(pr(ci(job(UBUNTU, 11, "fail", required=True), sibling)), fix_end(30, k(11))) == Wait(
        WaitReason.CI_PENDING)


@pytest.mark.parametrize("runs", [(), ((0, 1),), ((900, 0),), ((900, True),)])
def test_a_rerun_names_real_runs(runs):
    with pytest.raises(ValueError):
        Rerun("h1", runs)


# ── a resume is not an attempt ──────────────────────────────────────────────


def test_every_failure_of_one_try_resumes_it_at_its_tier_from_any_workflow():
    assert state(pr(SECOND), fix_end(30, k(11))).tier == escalate(DEFAULT_TIER, 0)
    # Another workflow's run on the same head and first attempt: the same try.
    other = ci(job(UBUNTU, 11, "fail", required=True), job("lint", 31, "fail", run=901))
    step = state(pr(other), fix_end(30, k(11)))
    assert isinstance(step, Dispatch) and step.evidence.keys == (k(31, run=901),)
    assert step.tier == escalate(DEFAULT_TIER, 0)
    # Two earlier pushes, then a failure later in this head's try: two attempts, past the cap's reach.
    tried = (fix_end(10, k(1), head="ha"), fix_end(20, k(2), head="hb"), fix_end(30, k(11)))
    resumed = state(pr(SECOND), *tried)
    assert isinstance(resumed, Dispatch) and resumed.evidence.keys == (k(12),)
    assert resumed.tier == escalate(DEFAULT_TIER, 2)


def test_attempt_cap_bounds_pushes_and_reruns():
    tried = (fix_end(10, k(1), head="ha"), fix_end(20, k(2), head="hb"), fix_end(30, k(11)))
    assert state(pr(RERUN), *tried) == stop(30)  # the third try's re-run would be a fourth


def test_an_earlier_resume_is_not_counted_towards_the_cap():
    # Push (h0), try on h1 (k11) and its resume (k12): two tries, so the re-run is bought as the third.
    legs = (fix_end(20, k(2), head="h0"), fix_end(30, k(11)), fix_end(40, k(12)))
    step = state(pr(RERUN), *legs)
    assert isinstance(step, Dispatch) and step.tier == escalate(DEFAULT_TIER, 2)


def test_a_leg_without_readable_keys_is_counted_so_it_cannot_loop_uncounted():
    # A FIX_CI leg from before receipts recorded keys, or a corrupt one, is a try of its own; the next leg
    # resumes it, but a run of them climbs and stops at the cap.
    legacy = state(pr(SECOND), fix_end(30))
    assert isinstance(legacy, Dispatch) and legacy.tier == escalate(DEFAULT_TIER, 0)
    keyless = tuple(fix_end(m) for m in (10, 20, 30))
    assert state(pr(SECOND), *keyless).tier == escalate(DEFAULT_TIER, 2)
    assert state(pr(SECOND), fix_end(0), *keyless) == stop(30)
    unreadable = (fix_end(10, "check:1@"), fix_end(20, "check:2@"), fix_end(30, "check:3@"))  # keys naming no time
    assert state(pr(SECOND), fix_end(0, k(1)), *unreadable) == stop(30)


def test_a_status_of_the_same_try_resumes_it():
    status = Check("sonar", "FAILURE", "fail", started_at="2026-10-14T00:55:00Z", kind="status")
    step = state(pr(ci(job(UBUNTU, 11, "fail", required=True), status)), fix_end(30, k(11)))
    assert isinstance(step, Dispatch) and step.tier == escalate(DEFAULT_TIER, 0)


def test_a_run_dispatched_during_or_after_a_leg_is_a_counted_try():
    # The agent's `gh workflow run`, a reopen or a label-triggered workflow: a run that began after the leg began.
    for minute in (29.5, 31):
        later = ci(job(UBUNTU, 11, "fail", required=True), job("lint", 41, "fail", run=902, start=at(minute).isoformat()))
        step = state(pr(later), fix_end(30, k(11)))
        assert isinstance(step, Dispatch) and step.evidence.keys == (k(41, start=at(minute).isoformat()),)
        assert step.tier == escalate(DEFAULT_TIER, 1)
    # Each new run is another try: the cap stops a loop of them.
    runs = tuple(fix_end(10 * n, k(40 + n, start=at(10 * n - 2).isoformat())) for n in (1, 2, 3))
    looping = ci(job(UBUNTU, 44, "fail", required=True, run=904, start=at(31).isoformat()))
    assert state(pr(looping), *runs) == stop(30)


def test_a_new_run_handed_legs_later_is_still_a_counted_try():
    # Each leg sets off a run that fails only after the next leg was dispatched: counted against the head's
    # first leg and the runs already handed, never just the previous leg, so the loop still stops.
    def lag(n: int, start: str) -> LegEnd:
        return fix_end(10 * n, k(n, start=start))

    legs = (lag(1, PUSHED), lag(2, PUSHED), *(lag(n, at(10 * (n - 2) - 0.5).isoformat()) for n in (3, 4)))
    late = ci(job(UBUNTU, 99, "fail", required=True, run=950, start=at(29.5).isoformat()))
    assert state(pr(late), *legs) == stop(40)
    # A later job of a run an earlier leg was handed resumes, however late it fails.
    sibling = ci(job(UBUNTU, 98, "fail", required=True, run=903, start=at(9.5).isoformat()))
    step = state(pr(sibling), lag(1, PUSHED), lag(2, PUSHED), lag(3, at(9.5).isoformat()))
    assert isinstance(step, Dispatch) and step.tier == escalate(DEFAULT_TIER, 1)


def test_a_push_is_a_new_attempt():
    pushed = ci(job(UBUNTU, 11, "fail", required=True), head="h2")  # same ids, impossible but proves the key is per head
    step = state(pr(pushed, head="h2"), fix_end(30, k(11), head="h1"))
    assert isinstance(step, Dispatch) and step.head == "h2" and step.evidence.keys == (k(11),)
    assert step.tier == escalate(DEFAULT_TIER, 1)


# ── a per-leg cap is no finished repair (#411 semantics, owner 2026-10-14) ──


def capped_pair(minute: float, *keys: str) -> tuple[LegEnd, LegEnd]:
    """A first cap hit (a machine end) and its resume, capped again (an agent end)."""
    return fix_end(minute - 5, machine=True), fix_end(minute, *keys, capped=True)


def test_a_repair_capped_on_its_resume_hands_the_same_failure_to_a_leg_one_tier_up():
    # The run is complete and the failure was handed: a finished repair would end in the re-run.
    step = state(pr(DONE), *capped_pair(30, k(11)))
    assert isinstance(step, Dispatch) and step.act is AgentKind.FIX_CI
    assert step.evidence == CiFailures((CiFailure(k(11), UBUNTU, 11, 900),))  # not "handled": nobody finished it
    assert step.tier == escalate(DEFAULT_TIER, 1)


def test_a_repair_that_ended_normally_having_pushed_nothing_still_ends_in_the_rerun_then_a_stop():
    assert state(pr(DONE), fix_end(30, k(11))) == Rerun("h1", ((900, 1),))
    assert state(pr(DONE), fix_end(25, machine=True), fix_end(30, k(11))) == Rerun("h1", ((900, 1),))  # a resume
    # The fresh leg after a capped one finishes: the existing path resumes.
    assert state(pr(DONE), *capped_pair(30, k(11)), fix_end(40, k(11))) == Rerun("h1", ((900, 1),))
    assert state(pr(RERUN), *capped_pair(30, k(11)), fix_end(40, k(11)), fix_end(50, k(21, attempt=2))) == stop(50)


def test_capped_resumes_climb_one_tier_each_and_stop_at_the_attempt_cap():
    two = (*capped_pair(20, k(11)), *capped_pair(30, k(11)))
    climbed = state(pr(DONE), *two)
    assert isinstance(climbed, Dispatch) and climbed.tier == escalate(DEFAULT_TIER, 2)
    assert climbed.evidence.keys == (k(11),)
    # The first cap of the next try is still no attempt: its resume runs at that try's tier.
    resumed = state(pr(DONE), *two, fix_end(35, machine=True))
    assert isinstance(resumed, Dispatch) and resumed.tier == escalate(DEFAULT_TIER, 2)
    assert state(pr(DONE), *two, *capped_pair(40, k(11))) == stop(40)


def test_a_capped_continuation_of_a_try_ends_that_try():
    # A finished leg handed k11; the same run's later failure k12 continues its try at its tier and is capped.
    legs = (fix_end(20, k(11)), fix_end(25, machine=True), fix_end(30, k(12), capped=True))
    step = state(pr(SECOND), *legs)
    assert isinstance(step, Dispatch) and step.evidence.keys == (k(12),) and step.evidence.handled == (UBUNTU,)
    assert step.tier == escalate(DEFAULT_TIER, 1)


def test_a_capped_leg_that_pushed_leaves_its_old_heads_evidence_behind():
    # The cap fell after a push: the new head's CI is a new try, as any push is.
    legs = (fix_end(25, machine=True), fix_end(30, k(11), capped=True, end_head="h2"))
    assert state(pr(DONE), *legs) == Wait(WaitReason.CI_UNKNOWN)  # read-your-writes
    pushed = ci(job(UBUNTU, 31, "fail", required=True, start=at(31).isoformat()), head="h2")
    step = state(pr(pushed, head="h2"), *legs)
    assert isinstance(step, Dispatch) and step.head == "h2" and step.tier == escalate(DEFAULT_TIER, 1)


@pytest.mark.parametrize("bad", [
    dict(capped=True, machine=True),
    dict(capped=True, question="404 or 410?"),
    dict(capped="yes"), dict(capped=1), dict(capped=None),
])
def test_a_capped_end_is_an_agent_end_that_asked_nothing(bad):
    with pytest.raises(ValueError):
        LegEnd(AgentKind.FIX_CI, at(30), bad.pop("machine", False), "h1", **bad)


def test_a_machine_end_was_handed_nothing_so_the_evidence_is_retried_after_the_backoff():
    refused = fix_end(30, head="h1", machine=True)
    assert state(pr(FIRST), refused, now=30.5) == Backoff(AgentKind.FIX_CI, at(31))
    step = state(pr(FIRST), refused, now=32)
    assert isinstance(step, Dispatch) and step.evidence.keys == (k(11),)


def test_a_human_comment_re_arms_handed_evidence():
    nudged = pr(RERUN, comments=(Comment("alice", "try again", at(45).isoformat()),))
    step = state(nudged, fix_end(30, k(11)), fix_end(40, k(21, attempt=2)))
    assert isinstance(step, Dispatch) and step.evidence.keys == (k(21, attempt=2),)


def test_a_green_head_is_not_a_repair_however_its_optional_jobs_fail():
    assert state(pr(ci(job(UBUNTU, 11, "pass", required=True), job(WINDOWS, 12, "fail")))) == Dispatch(
        AgentKind.REVIEW, "h1")


# ── typed evidence: an empty repair is unrepresentable ─────────────────────

ONE = CiFailures((CiFailure(k(11), UBUNTU, 11, 900),))


@pytest.mark.parametrize("build", [
    lambda: RunAgent(AgentKind.FIX_CI, 7, head="h1"),
    lambda: RunAgent(AgentKind.ADDRESS_REVIEW, 7, head="h1"),
    lambda: RunAgent(AgentKind.FIX_CI, 7, head="h1", evidence=ChangeRequest("rev", "h1")),
    lambda: RunAgent(AgentKind.REBASE, 7, head="h1", evidence=ONE),
    lambda: Dispatch(AgentKind.FIX_CI, "h1"),
    lambda: Dispatch(AgentKind.ADDRESS_REVIEW, "h1", evidence=ONE),
    lambda: CiFailures(()),
    lambda: CiFailures((CiFailure("k", "a"), CiFailure("k", "b"))),
    lambda: CiFailure("", "a"),
    lambda: CiFailure("k", ""),
    lambda: CiFailure("k" * 513, "a"),
    lambda: CiFailure("k", "a", 0, 900),
    lambda: CiFailure("k", "a", True, 900),
    lambda: CiFailure("k", "a", 11),  # a job without its run
    lambda: ChangeRequest("", "h1"),
    lambda: ChangeRequest("rev", ""),
    lambda: ChangeRequest("rev", "h1", 0),
    lambda: ChangeRequest("rev", "h1", body=None),
])
def test_an_evidence_less_or_malformed_repair_cannot_be_built(build):
    with pytest.raises(ValueError):
        build()


def test_a_humans_standing_request_is_carried_not_the_bots_newer_one():
    reviews = (Review("CHANGES_REQUESTED", "alice", "2026-10-14T01:00:00Z", "Rename it.", "h1", id=1),
               Review("CHANGES_REQUESTED", "review-bot", "2026-10-14T01:05:00Z", "bot", "h1", id=2))
    step = state(pr(ci(job(UBUNTU, 11, "pass", required=True)), reviews=reviews))
    assert step == Dispatch(AgentKind.ADDRESS_REVIEW, "h1", evidence=ChangeRequest("alice", "h1", 1, "Rename it."))


def test_a_withdrawn_human_request_is_not_carried():
    reviews = (Review("CHANGES_REQUESTED", "alice", "2026-10-14T01:00:00Z", "Rename it.", "h1", id=1),
               Review("CHANGES_REQUESTED", "bob", "2026-10-14T01:10:00Z", "Nit.", "h1", id=2),
               Review("APPROVED", "bob", "2026-10-14T01:20:00Z", "", "h1", id=3))
    step = state(pr(ci(job(UBUNTU, 11, "pass", required=True)), reviews=reviews))
    assert isinstance(step, Dispatch) and step.evidence == ChangeRequest("alice", "h1", 1, "Rename it.")


def test_address_review_carries_the_newest_change_request_on_the_head_even_without_a_body():
    reviews = (Review("CHANGES_REQUESTED", "review-bot", "2026-10-14T01:00:00Z", "old", "h1", id=1),
               Review("CHANGES_REQUESTED", "review-bot", "2026-10-14T01:05:00Z", "", "h1", id=2),
               Review("CHANGES_REQUESTED", "alice", "2026-10-14T01:09:00Z", "stale head", "h0", id=3))
    step = state(pr(ci(job(UBUNTU, 11, "pass", required=True)), reviews=reviews))
    assert step == Dispatch(AgentKind.ADDRESS_REVIEW, "h1", evidence=ChangeRequest("review-bot", "h1", 2, ""))


# ── malformed, absent and huge evidence ─────────────────────────────────────


def test_a_status_or_another_apps_check_is_handed_by_name_and_keyed_by_its_post():
    status = Check("sonar", "FAILURE", "fail", started_at="2026-10-14T01:00:00Z", required=True, kind="status")
    app = Check("codecov", "FAILURE", "fail", check_id=55)  # no Actions run: no job log to point at
    fresh = new_failures(Observed("h1", "h1", frozenset(), (status, app)), ())
    assert fresh is not None and [(f.name, f.job_id, f.run_id) for f in fresh.failures] == [
        ("sonar", None, None), ("codecov", None, None)]
    reposted = Check("sonar", "FAILURE", "fail", started_at="2026-10-14T02:00:00Z", required=True, kind="status")
    again = new_failures(Observed("h1", "h1", frozenset(), (reposted,)), fresh.keys)
    assert again is not None and again.failures[0].name == "sonar"


def test_a_huge_failing_matrix_is_bounded():
    matrix = tuple(job(f"shard {i:04}", 1000 + i, "fail") for i in range(MAX_FAILURES * 4))
    fresh = new_failures(ci(*matrix), ())
    assert fresh is not None and len(fresh.failures) == MAX_FAILURES
    rest = new_failures(ci(*matrix), fresh.keys)
    assert rest is not None and not set(rest.keys) & set(fresh.keys)


def test_required_failures_come_first():
    optional = tuple(job(f"a{i}", 100 + i, "fail") for i in range(3))
    fresh = new_failures(ci(*optional, job("z-required", 7, "fail", required=True)), ())
    assert fresh is not None and fresh.failures[0] == CiFailure(k(7), "z-required", 7, 900)


@pytest.mark.parametrize("key,at_", [
    (k(1), datetime(2026, 10, 14, 0, 50, tzinfo=timezone.utc)), ("check:1", None), ("check:1@", None),
    ("status:a@b@2026-10-14T01:00:00Z", T0), ("status:x@2026#900", None), ("", None), ("check:1@" + "9" * 600, None),
])
def test_began_reads_the_time_a_key_names_or_none(key, at_):
    assert began(key) == at_


@pytest.mark.parametrize("raw,keys", [
    (None, frozenset()),
    ("check:1", frozenset()),
    ({"check:1": True}, frozenset()),
    (["check:1", 2, None, "", "k" * 513, "check:2"], frozenset({"check:1", "check:2"})),
    ([f"check:{i}" for i in range(MAX_FAILURES * 10)], frozenset(f"check:{i}" for i in range(MAX_FAILURES))),
])
def test_recorded_keys_read_back_tolerates_malformed_and_huge_receipts(raw, keys):
    assert recorded_keys(raw) == keys


def test_only_a_fix_ci_agent_end_records_handed_keys():
    for bad in (dict(kind=AgentKind.REBASE), dict(machine=True)):
        args = {"kind": AgentKind.FIX_CI, "ended_at": T0, "machine": False, "evidence": frozenset({"check:1"}), **bad}
        with pytest.raises(ValueError):
            LegEnd(**args)
