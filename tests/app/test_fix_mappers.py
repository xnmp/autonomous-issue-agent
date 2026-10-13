"""Pure mappers of the tick (slices 5b/5c): recorded legs of either role to
derive's leg ends, leases, the fix role's dispatch budget per tick, and the
human reply text."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.control_store import LegRun
from issue_agent.app.fix import fix_actions, human_reply
from issue_agent.app.tick import Facts, leased_items, leg_end, local_legs, run_item
from issue_agent.domain.actions import AgentKind, MergePullRequest, RunAgent
from issue_agent.domain.backoff import LegEnd
from issue_agent.domain.checks import Check, Observed, RequiredCheck
from issue_agent.domain.labels import Label
from issue_agent.domain.state import Harness, Local, derive, issue_key, pr_key
from issue_agent.domain.world import Comment, IssueView, PRView, Review, WorldState

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
H = Harness("fix-bot", "review-bot")


def run(kind="RESOLVE", number=5, issue=5, head="", leg="agent", minute=1, started=0, **kw) -> LegRun:
    receipt = kw.pop("receipt", {"leg": leg, "session": "s1", "runtime": "claude"} if leg else None)
    start = T0 + timedelta(minutes=started) if started is not None else None
    return LegRun(kind, number, issue, head, start, T0 + timedelta(minutes=minute), receipt)


# ── recorded runs → leg ends ─────────────────────────────────────────────────

@pytest.mark.parametrize("recorded,item", [
    (run("RESOLVE"), issue_key(5)),
    (run("RESUME"), None),               # pre-5b kinds stop counting at the cutover
    (run("FIX_CI", number=100), pr_key(100)),
    (run("PR_RESUME", number=100), None),
    (run("ROUTE"), None),                # the deleted classifier leg belongs to no item
    (run("RESOLVE", issue=0), None),
    (run("REVIEW", number=100), pr_key(100)),  # both roles derive from every leg
    (run("FIX_CI", number=0), None),
])
def test_each_run_belongs_to_its_issue_or_pr_item(recorded, item):
    assert run_item(recorded) == item


def test_a_returned_agent_leg_is_one_attempt_with_its_window():
    end = leg_end(run("FIX_CI", number=100, head="h1", leg="agent", started=0, minute=3))
    assert end == LegEnd(AgentKind.FIX_CI, T0 + timedelta(minutes=3), False, "h1", started_at=T0)


@pytest.mark.parametrize("receipt", [None, {}, {"leg": "machine"}, {"leg": "AGENT"}, {"leg": 1}])
def test_a_run_without_an_agent_receipt_is_a_machine_end(receipt):
    end = leg_end(run(receipt=receipt))
    assert end is not None and end.machine and end.question is None


def test_an_asked_receipt_is_an_agent_end_carrying_its_question():
    end = leg_end(run(receipt={"leg": "asked", "question": "404 or 410?"}))
    assert end is not None and (end.machine, end.question) == (False, "404 or 410?")


@pytest.mark.parametrize("question", [None, "", 7, "<!-- needs-human:attempts -->"])
def test_an_asked_receipt_with_a_missing_or_hostile_question_still_stops_safely(question):
    end = leg_end(run(receipt={"leg": "asked", **({} if question is None else {"question": question})}))
    assert end is not None and end.question and "<!--" not in end.question


def test_an_agent_receipt_carries_no_question():
    end = leg_end(run(receipt={"leg": "agent", "question": "stale"}))
    assert end is not None and end.question is None


@pytest.mark.parametrize("kind", ["RESUME", "PR_RESUME", "ROUTE", "RE_REVIEW", "UNKNOWN_KIND", ""])
def test_runs_of_no_act_are_no_leg_end(kind):
    assert leg_end(run(kind)) is None


def test_an_inconsistent_start_is_dropped_but_the_end_still_counts():
    end = leg_end(run(started=10, minute=3))  # started after it ended
    assert end is not None and end.started_at is None


def test_local_legs_group_both_roles_legs_by_item_in_end_order():
    legs = local_legs([run("FIX_CI", number=100, minute=3), run("RESOLVE", minute=1),
                       run("FIX_CI", number=100, minute=2), run("REVIEW", number=100, minute=4)])
    assert set(legs) == {issue_key(5), pr_key(100)}
    assert [(e.kind, e.ended_at.minute) for e in legs[pr_key(100)]] == [
        (AgentKind.FIX_CI, 2), (AgentKind.FIX_CI, 3), (AgentKind.REVIEW, 4)]


def test_leases_cover_own_dispatches_and_other_holders_issues_only():
    held = leased_items([RunAgent(AgentKind.RESOLVE, 5), RunAgent(AgentKind.FIX_CI, 100, head="h1")],
                        [7, 0, -3, "8", True, None])
    assert held == frozenset({issue_key(5), pr_key(100), issue_key(7)})


# ── the tick's actions ───────────────────────────────────────────────────────

def ci(head="h1", bucket="pass"):
    return Observed(head, head, frozenset({RequiredCheck("test")}),
                    (Check("test", state="COMPLETED", bucket=bucket, required=True),))


def harness_pr(number=100, issue=5, **kw):
    kw.setdefault("ci", ci())
    return PRView(number=number, head_ref_name=f"fix/{issue}-x", head_ref_oid="h1", state="OPEN",
                  mergeable="MERGEABLE", merge_state="CLEAN", **kw)


def local(**kw):
    return Local(now=T0 + timedelta(days=1), legs={}, **kw)


def test_paid_dispatches_fit_the_free_slots_issues_first():
    world = WorldState(issues=tuple(IssueView(n, labels=frozenset({Label.AI_READY})) for n in (3, 1, 2)),
                       prs=(harness_pr(100, issue=9, ci=ci(bucket="fail")),))
    chosen = fix_actions(derive(world, local(), H), H, slots=2)
    assert chosen == (RunAgent(AgentKind.RESOLVE, 1), RunAgent(AgentKind.RESOLVE, 2))
    assert fix_actions(derive(world, local(), H), H, slots=0) == ()


def test_free_actions_ignore_the_slot_budget_and_review_is_never_the_fix_roles():
    approved = harness_pr(100, reviews=(Review("APPROVED", "review-bot", commit_id="h1"),))
    unreviewed = harness_pr(101, issue=6)
    chosen = fix_actions(derive(WorldState(prs=(approved, unreviewed)), local(), H), H, slots=0)
    assert chosen == (MergePullRequest(100, "h1"),)
    assert fix_actions(derive(WorldState(prs=(approved, unreviewed)), local(), H), H, slots=5, merge=False) == ()


@pytest.mark.parametrize("slots", [-1, 0])
def test_no_slots_means_no_paid_dispatch(slots):
    world = WorldState(issues=(IssueView(1, labels=frozenset({Label.AI_READY})),))
    assert fix_actions(derive(world, local(), H), H, slots=slots) == ()


# ── reply and question text ──────────────────────────────────────────────────

def test_the_reply_is_every_human_comment_after_the_leg_in_order():
    after = T0 + timedelta(minutes=5)
    comments = (
        Comment("alice", "too early", (T0 + timedelta(minutes=4)).isoformat()),
        Comment("bob", "second", (T0 + timedelta(minutes=7)).isoformat()),
        Comment("alice", "first", (T0 + timedelta(minutes=6)).isoformat()),
        Comment("fix-bot", f"{H.marker} harness note", (T0 + timedelta(minutes=8)).isoformat()),
        Comment("ci[bot]", "coverage", (T0 + timedelta(minutes=9)).isoformat()),
        Comment("carol", "undated", "yesterday"),
    )
    assert human_reply(comments, after, H) == "@alice: first\n\n@bob: second"


def test_no_reply_is_empty():
    assert human_reply((), T0, H) == ""



# ── Facts (round 2) ───────────────────────────────────────────────────────

class _Control:
    def leg_runs(self, repository, issues):
        return ()

    def held_targets(self, repository):
        return frozenset()


def test_an_unreadable_lifetime_read_holds_paid_work_and_says_so():
    def unreadable(_issue):
        raise RuntimeError("spend snapshot unavailable")

    lines: list[str] = []
    facts = Facts(_Control(), "o/r", active=lambda: (),
                     headroom=lambda: True, lifetime_capped=unreadable, log=lines.append, now=lambda: T0)
    loc = facts(WorldState(issues=(IssueView(5, labels=frozenset({Label.AI_READY})),)))
    assert loc.headroom is False and loc.lifetime_capped == frozenset()
    assert len(lines) == 1 and "spend snapshot unavailable" in lines[0]
