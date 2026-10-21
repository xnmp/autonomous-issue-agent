"""#441, through derive: each way a leg ends, recorded as the accounting
runtime records it, read by `tick.leg_end` and decided by `derive`. Attempt
caps protect money; a cut-off end is free once per try, then counts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.control_store import LegRun
from issue_agent.app.tick import leg_ends, local_legs
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.attempt import Continue, Fresh, next_leg
from issue_agent.domain.checks import Check, Observed, RequiredCheck
from issue_agent.domain.labels import Label
from issue_agent.domain.routing import DEFAULT_TIER, escalate
from issue_agent.domain.state import (
    ATTEMPT_CAP, Backoff, Dispatch, Harness, Local, NeedsHuman, QuestionStop, StopReason, issue_key, pr_key,
)
from tests.fakes.recorded import derive
from issue_agent.domain.world import IssueView, PRView, WorldState

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
H = Harness("fix-bot", "review-bot")
CUT_OFFS = ("wall_timeout", "budget_exhausted", "no_result")
LATER = 24 * 60  # past every machine backoff


def leg(kind: str, minute: int, receipt: dict | None, *, number: int = 3, head: str = "") -> LegRun:
    return LegRun(kind, number, 3, head, T0 + timedelta(minutes=minute), T0 + timedelta(minutes=minute, seconds=30),
                  receipt, f"{kind}-{minute}")


def agent(**extra) -> dict:
    return {"leg": "agent", "session": "s-1", "runtime": "claude", **extra}


def cut(end: str = "wall_timeout", session: str = "s-1", runtime: str = "claude", **extra) -> dict:
    """A cut-off end as the accounting runtime records it since #441."""
    return {"leg": "machine", "end": end, "session": session, "runtime": runtime, **extra}


# A stewarded, triaged issue: its next act is RESOLVE at the default tier.
TRIAGED = (leg("STEWARD", -120, agent()), leg("TRIAGE", -60, agent()))


def resolve_state(*runs: LegRun, now: int = LATER):
    world = WorldState(issues=(IssueView(3, labels=frozenset({Label.AI_READY})),))
    moment = T0 + timedelta(minutes=now)
    return derive(world, Local(now=moment, legs=local_legs((*TRIAGED, *runs))), H)[issue_key(3)]


def resolve_plan(*runs: LegRun, runtime: str = "claude"):
    return next_leg(local_legs(runs).get(issue_key(3), ()), runtime)


# ── routed acts: one free resume per try ──────────────────────────────────────


@pytest.mark.parametrize("end", CUT_OFFS)
def test_a_first_cut_off_is_no_attempt_and_its_session_is_continued(end):
    first = leg("RESOLVE", 0, cut(end))
    assert resolve_state(first, now=1) == Backoff(AgentKind.RESOLVE, T0 + timedelta(minutes=1, seconds=30))
    assert resolve_state(first) == Dispatch(AgentKind.RESOLVE, tier=DEFAULT_TIER)
    assert resolve_plan(first).start == Continue("s-1")


@pytest.mark.parametrize("first", CUT_OFFS)
@pytest.mark.parametrize("second", CUT_OFFS)
def test_a_cut_off_of_the_free_resume_is_an_attempt_and_the_next_try_is_fresh_one_tier_up(first, second):
    runs = (leg("RESOLVE", 0, cut(first)), leg("RESOLVE", 5, cut(second)))
    assert resolve_state(*runs) == Dispatch(AgentKind.RESOLVE, tier=escalate(DEFAULT_TIER, 1))
    assert resolve_plan(*runs).start == Fresh()


def test_each_try_gets_one_free_resume_and_the_attempt_cap_stops_a_run_of_them():
    runs, minute = [], 0
    for _ in range(ATTEMPT_CAP):
        runs += [leg("RESOLVE", minute, cut(session=f"s-{minute}")), leg("RESOLVE", minute + 5, cut(session=f"s-{minute}"))]
        minute += 10
    assert [end.machine for _, end in leg_ends(runs)] == [True, False] * ATTEMPT_CAP
    state = resolve_state(*runs)
    assert isinstance(state, NeedsHuman) and state.reason is StopReason.ATTEMPTS


def test_a_receipt_less_death_between_cut_offs_does_not_reset_the_free_resume():
    runs = (leg("RESOLVE", 0, cut()), leg("RESOLVE", 5, None), leg("RESOLVE", 10, cut()))
    assert resolve_state(*runs) == Dispatch(AgentKind.RESOLVE, tier=escalate(DEFAULT_TIER, 1))


@pytest.mark.parametrize("end,session,counts", [
    ("transport_death", "", False),  # the session died with it: nothing was continued
    ("runtime_unavailable", "", False),
    ("operator_cancelled", "s-1", True),  # a teardown leaves its session, which the next leg continues
])
def test_a_cut_off_after_another_machine_end_counts_only_if_it_continued_that_session(end, session, counts):
    runs = (leg("RESOLVE", 0, {"leg": "machine", "end": end, "session": session, "runtime": "claude"}),
            leg("RESOLVE", 5, cut()))
    assert resolve_state(*runs) == Dispatch(AgentKind.RESOLVE, tier=escalate(DEFAULT_TIER, int(counts)))


def test_a_cut_off_session_on_another_runtime_is_not_continued_so_its_successor_is_free():
    runs = (leg("RESOLVE", 0, cut(runtime="codex")), leg("RESOLVE", 5, cut(session="s-2")))
    assert [end.machine for _, end in leg_ends(runs)] == [True, True]
    assert resolve_plan(*runs, runtime="codex").start == Continue("s-1")


def test_an_infra_death_and_a_teardown_are_never_attempts():
    runs = (leg("RESOLVE", 0, {"leg": "machine", "end": "transport_death", "runtime": "claude"}),
            leg("RESOLVE", 5, {"leg": "machine", "end": "operator_cancelled", "session": "s-1", "runtime": "claude"}),
            leg("RESOLVE", 10, {"leg": "machine", "end": "killed_externally", "runtime": "claude"}))
    assert [end.machine for _, end in leg_ends(runs)] == [True] * 3
    assert resolve_state(*runs) == Dispatch(AgentKind.RESOLVE, tier=DEFAULT_TIER)


def test_an_agent_end_is_one_attempt_and_a_question_stops_for_a_human():
    assert resolve_state(leg("RESOLVE", 0, agent())) == Dispatch(AgentKind.RESOLVE, tier=escalate(DEFAULT_TIER, 1))
    state = resolve_state(leg("RESOLVE", 0, {"leg": "asked", "question": "404 or 410?", "session": "s-q",
                                             "runtime": "claude"}))
    assert isinstance(state, QuestionStop) and state.question == "404 or 410?"


def test_a_cut_off_answer_is_continued_with_the_question_still_answered():
    asked = leg("RESOLVE", 0, {"leg": "asked", "question": "404?", "session": "s-q", "runtime": "claude"})
    plan = resolve_plan(asked, leg("RESOLVE", 5, cut(session="s-q")))
    assert plan.start == Continue("s-q") and plan.answering is not None and plan.answering.question == "404?"


# ── kinds that never resume: a cut-off counts at once ─────────────────────────


@pytest.mark.parametrize("end", CUT_OFFS)
def test_a_cut_off_steward_or_triage_is_their_one_attempt_not_a_free_loop(end):
    stewarded = (leg("STEWARD", -120, cut(end)),)
    world = WorldState(issues=(IssueView(3, labels=frozenset({Label.AI_READY})),))
    later = T0 + timedelta(minutes=LATER)
    assert derive(world, Local(now=later, legs=local_legs(stewarded)), H)[issue_key(3)] == Dispatch(AgentKind.TRIAGE)
    triaged = (leg("STEWARD", -120, agent()), leg("TRIAGE", -60, cut(end)))
    assert derive(world, Local(now=later, legs=local_legs(triaged)), H)[issue_key(3)] == Dispatch(
        AgentKind.RESOLVE, tier=DEFAULT_TIER)


def _green_pr() -> PRView:
    check = Check("test", "SUCCESS", "pass", required=True)
    return PRView(number=7, head_ref_name="fix/3-x", head_ref_oid="h1", mergeable="MERGEABLE", merge_state="CLEAN",
                  ci=Observed("h1", "h1", frozenset({RequiredCheck("test")}), (check,)))


def test_cut_off_reviews_count_each_and_stop_at_the_attempt_cap():
    runs = tuple(leg("REVIEW", m, cut(session=""), number=7, head="h1") for m in range(0, 10 * ATTEMPT_CAP, 10))
    later = T0 + timedelta(minutes=LATER)
    state = derive(WorldState(prs=(_green_pr(),)), Local(now=later, legs=local_legs(runs)), H)[pr_key(7)]
    assert isinstance(state, NeedsHuman) and state.reason is StopReason.ATTEMPTS


# ── FIX_CI: a cut-off attempt hands its failures on (#415) ───────────────────

KEY = "check:11@2026-10-01T11:50:00Z"


def _fix_ci_state(*receipts: dict):
    check = Check("test", "FAILURE", "fail", "2026-10-01T11:50:00Z", run_id=900, required=True, check_id=11,
                  run_attempt=1)
    view = PRView(number=7, head_ref_name="fix/3-x", head_ref_oid="h1", mergeable="MERGEABLE", merge_state="CLEAN",
                  ci=Observed("h1", "h1", frozenset({RequiredCheck("test")}), (check,)))
    runs = tuple(leg("FIX_CI", 5 * i, {**r, "evidence": [KEY]}, number=7, head="h1") for i, r in enumerate(receipts))
    later = T0 + timedelta(minutes=LATER)
    return derive(WorldState(prs=(view,)), Local(now=later, legs=local_legs(runs)), H)[pr_key(7)]


@pytest.mark.parametrize("second", CUT_OFFS)
def test_a_cut_off_resume_hands_its_failures_to_a_leg_one_tier_up(second):
    state = _fix_ci_state(cut(), cut(second))
    assert isinstance(state, Dispatch) and state.act is AgentKind.FIX_CI and state.tier is escalate(DEFAULT_TIER, 1)


# ── receipts written before #441 ──────────────────────────────────────────────


def test_a_pre_441_cap_record_reads_as_it_did():
    # A free resume's cap was a machine end marked `capped`; its resume's an agent end marked `capped`.
    old = (leg("RESOLVE", 0, {"leg": "machine", "capped": True, "session": "s-1", "runtime": "claude"}),
           leg("RESOLVE", 5, {"leg": "agent", "capped": True, "session": "s-1", "runtime": "claude"}))
    assert [(end.machine, end.capped) for _, end in leg_ends(old)] == [(True, False), (False, True)]
    assert resolve_plan(old[0]).start == Continue("s-1")
    assert resolve_state(*old) == Dispatch(AgentKind.RESOLVE, tier=escalate(DEFAULT_TIER, 1))


# ── malformed, absent and huge receipts ──────────────────────────────────────


@pytest.mark.parametrize("end", [None, 1, ["wall_timeout"], {"wall_timeout": 1}, "WALL_TIMEOUT", "x" * 100_000, ""])
def test_a_malformed_end_is_no_cut_off(end):
    runs = (leg("RESOLVE", 0, cut()), leg("RESOLVE", 5, {**cut(), "end": end}))
    assert [end.machine for _, end in leg_ends(runs)] == [True, True]
    assert all(e.end in ("", "wall_timeout") for _, e in leg_ends(runs))


@pytest.mark.parametrize("session", [None, 7, ["s-1"], "", "s" * 129])
def test_a_malformed_or_oversized_session_is_never_continued(session):
    runs = (leg("RESOLVE", 0, cut(session=session)),)  # type: ignore[arg-type]
    assert resolve_plan(*runs).start == Fresh()
    assert [end.session for _, end in leg_ends(runs)] == [""]


@pytest.mark.parametrize("runtime", [None, 3, "CLAUDE", "gpt", "c" * 1000])
def test_an_unknown_runtime_is_never_continued_or_counted_as_a_resume(runtime):
    runs = (leg("RESOLVE", 0, cut(runtime=runtime)), leg("RESOLVE", 5, cut(runtime=runtime)))  # type: ignore[arg-type]
    assert [end.machine for _, end in leg_ends(runs)] == [True, True]
    assert resolve_plan(*runs).start == Fresh()


@pytest.mark.parametrize("receipt", [None, {}, {"leg": "AGENT"}, {"leg": 1}, {"leg": ["agent"]}])
def test_an_absent_or_unreadable_class_is_a_machine_end(receipt):
    assert [end.machine for _, end in leg_ends((leg("RESOLVE", 0, receipt),))] == [True]


def test_a_huge_receipt_reads_by_its_classifying_keys_alone():
    junk = {f"k{i}": "v" * 100 for i in range(10_000)}
    runs = (leg("RESOLVE", 0, cut(**junk)), leg("RESOLVE", 5, cut(**junk)))
    assert [(end.machine, end.capped) for _, end in leg_ends(runs)] == [(True, False), (False, True)]


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("kind", ["RESOLVE", "FIX_CI", "ADDRESS_REVIEW", "REBASE", "STEWARD", "TRIAGE", "REVIEW"])
def test_provider_failures_never_spend_attempts_or_advance_the_workflow(kind, runtime):
    runs = tuple(leg(kind, 5 * i, cut("provider_refused", runtime=runtime),
                     number=7 if kind in ("REVIEW", "FIX_CI", "ADDRESS_REVIEW", "REBASE") else 3, head="h1")
                 for i in range(ATTEMPT_CAP + 2))
    assert all(end.machine and not end.capped for _, end in leg_ends(runs))
    if kind == "RESOLVE":
        assert resolve_state(*runs) == Dispatch(AgentKind.RESOLVE, tier=DEFAULT_TIER)
        assert resolve_plan(*runs, runtime=runtime).start == Continue("s-1")
    elif kind in ("STEWARD", "TRIAGE"):
        before = () if kind == "STEWARD" else (TRIAGED[0],)
        world = WorldState(issues=(IssueView(3, labels=frozenset({Label.AI_READY})),))
        state = derive(world, Local(now=T0 + timedelta(minutes=LATER), legs=local_legs((*before, *runs))), H)
        assert state[issue_key(3)] == Dispatch(AgentKind[kind])
    elif kind == "REVIEW":
        state = derive(WorldState(prs=(_green_pr(),)),
                       Local(now=T0 + timedelta(minutes=LATER), legs=local_legs(runs)), H)[pr_key(7)]
        assert isinstance(state, Dispatch) and state.act is AgentKind.REVIEW
    else:
        assert next_leg(local_legs(runs)[pr_key(7)], runtime).start == Continue("s-1")
        if kind == "FIX_CI":
            state = _fix_ci_state(*(cut("provider_refused", runtime=runtime) for _ in runs))
            assert isinstance(state, Dispatch) and state.act is AgentKind.FIX_CI and state.tier is DEFAULT_TIER
