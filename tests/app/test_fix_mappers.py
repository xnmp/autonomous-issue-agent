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
from issue_agent.domain.evidence import CiFailure, CiFailures
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


@pytest.mark.parametrize(
    "recorded,item",
    [
        (run("RESOLVE"), issue_key(5)),
        (run("RESUME"), None),  # pre-5b kinds stop counting at the cutover
        (run("FIX_CI", number=100), pr_key(100)),
        (run("PR_RESUME", number=100), None),
        (run("ROUTE"), None),  # the deleted classifier leg belongs to no item
        (run("RESOLVE", issue=0), None),
        (run("REVIEW", number=100), pr_key(100)),  # both roles derive from every leg
        (run("FIX_CI", number=0), None),
    ],
)
def test_each_run_belongs_to_its_issue_or_pr_item(recorded, item):
    assert run_item(recorded) == item


def test_a_returned_agent_leg_is_one_attempt_with_its_window():
    end = leg_end(run("FIX_CI", number=100, head="h1", leg="agent", started=0, minute=3))
    assert end == LegEnd(AgentKind.FIX_CI, T0 + timedelta(minutes=3), False, "h1", started_at=T0)


@pytest.mark.parametrize("kind,leg,raw,keys", [
    ("FIX_CI", "agent", ["check:11", "check:12"], frozenset({"check:11", "check:12"})),
    ("FIX_CI", "agent", "check:11", frozenset()),  # malformed: nothing handed, so it is handed again
    ("FIX_CI", "machine", ["check:11"], frozenset()),  # a machine end handed nothing
    ("REBASE", "agent", ["check:11"], frozenset()),  # only a CI repair is handed failures
])
def test_a_fix_ci_agent_end_reads_back_the_failures_it_was_handed(kind, leg, raw, keys):
    end = leg_end(run(kind, number=100, head="h1", receipt={"leg": leg, "evidence": raw}))
    assert end is not None and end.evidence == keys


@pytest.mark.parametrize("leg,raw,head", [
    ("agent", "h2", "h2"),
    ("agent", None, ""), ("agent", 7, ""), ("agent", ["h2"], ""), ("agent", "f" * 65, ""),  # malformed: unread
    ("machine", "h2", ""),  # a machine end recorded no receipt worth reading
])
def test_an_agent_end_reads_back_the_remote_head_it_left(leg, raw, head):
    end = leg_end(run("FIX_CI", number=100, head="h1", receipt={"leg": leg, "end_head": raw}))
    assert end is not None and end.end_head == head


@pytest.mark.parametrize("receipt", [None, {}, {"leg": "machine"}, {"leg": "AGENT"}, {"leg": 1}])
def test_a_run_without_an_agent_receipt_is_a_machine_end(receipt):
    end = leg_end(run(receipt=receipt))
    assert end is not None and end.machine and end.question is None


def test_a_recorded_steward_claim_on_the_issue_itself_is_dropped_and_logged():
    claim = {"acceptance_criteria": ["A"], "duplicate_of": None, "duplicate_reason": "",
             "blocked_by": 5, "blocked_by_reason": "Needs #5"}
    lines: list[str] = []
    end = leg_end(run("STEWARD", receipt={"leg": "agent", "steward": claim}), lines.append)
    assert end is not None and end.steward is not None and end.steward.blocked_by is None
    assert end.steward.acceptance_criteria == ("A",) and len(lines) == 1 and "#5" in lines[0]


def test_an_asked_receipt_is_an_agent_end_carrying_its_question():
    end = leg_end(run(receipt={"leg": "asked", "question": "404 or 410?"}))
    assert end is not None and (end.machine, end.question) == (False, "404 or 410?")


@pytest.mark.parametrize("question", [None, "", 7, "<!-- needs-human:attempts -->"])
def test_an_asked_receipt_with_a_missing_or_hostile_question_still_stops_safely(question):
    end = leg_end(run(receipt={"leg": "asked", **({} if question is None else {"question": question})}))
    assert end is not None and end.question and "<!--" not in end.question


@pytest.mark.parametrize("kind", ["RESUME", "PR_RESUME", "ROUTE", "RE_REVIEW", "UNKNOWN_KIND", ""])
def test_runs_of_no_act_are_no_leg_end(kind):
    assert leg_end(run(kind)) is None


def test_an_inconsistent_start_is_dropped_but_the_end_still_counts():
    end = leg_end(run(started=10, minute=3))  # started after it ended
    assert end is not None and end.started_at is None


def test_local_legs_group_both_roles_legs_by_item_in_end_order():
    legs = local_legs(
        [
            run("FIX_CI", number=100, minute=3),
            run("RESOLVE", minute=1),
            run("FIX_CI", number=100, minute=2),
            run("REVIEW", number=100, minute=4),
        ]
    )
    assert set(legs) == {issue_key(5), pr_key(100)}
    assert [(e.kind, e.ended_at.minute) for e in legs[pr_key(100)]] == [
        (AgentKind.FIX_CI, 2),
        (AgentKind.FIX_CI, 3),
        (AgentKind.REVIEW, 4),
    ]


_FAILED = CiFailures((CiFailure("check:1@9.1", "test", 1, 9),))


def test_leases_cover_own_dispatches_and_other_holders_issues_only():
    held = leased_items(
        [RunAgent(AgentKind.RESOLVE, 5), RunAgent(AgentKind.FIX_CI, 100, head="h1", evidence=_FAILED)],
        [7, 0, -3, "8", True, None]
    )
    assert held == frozenset({issue_key(5), pr_key(100), issue_key(7)})


# ── the tick's actions ───────────────────────────────────────────────────────


def ci(head="h1", bucket="pass"):
    return Observed(
        head,
        head,
        frozenset({RequiredCheck("test")}),
        (Check("test", state="COMPLETED", bucket=bucket, required=True),),
    )


def harness_pr(number=100, issue=5, **kw):
    kw.setdefault("ci", ci())
    return PRView(
        number=number,
        head_ref_name=f"fix/{issue}-x",
        head_ref_oid="h1",
        state="OPEN",
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        **kw,
    )


def local(**kw):
    return Local(now=T0 + timedelta(days=1), **{"legs": {}, **kw})


def test_heavy_dispatches_fit_the_free_slots_and_the_light_acts_need_none():
    triage = tuple(RunAgent(AgentKind.STEWARD, n) for n in (1, 2, 3))
    world = WorldState(
        issues=tuple(IssueView(n, labels=frozenset({Label.AI_READY})) for n in (3, 1, 2)),
        prs=(harness_pr(100, issue=9, ci=ci(bucket="fail")), harness_pr(101, issue=8, ci=ci(bucket="fail"))),
    )
    chosen = fix_actions(derive(world, local(), H), H, slots=1)
    assert chosen[:3] == triage and [a.kind for a in chosen[3:]] == [AgentKind.FIX_CI]
    assert fix_actions(derive(world, local(), H), H, slots=0) == triage


def test_free_actions_ignore_the_slot_budget_and_review_is_never_the_fix_roles():
    approved = harness_pr(100, reviews=(Review("APPROVED", "review-bot", commit_id="h1"),))
    unreviewed = harness_pr(101, issue=6)
    chosen = fix_actions(derive(WorldState(prs=(approved, unreviewed)), local(), H), H, slots=0)
    assert chosen == (MergePullRequest(100, "h1"),)
    assert fix_actions(derive(WorldState(prs=(approved, unreviewed)), local(), H), H, slots=5, merge=False) == ()


def test_the_light_acts_are_bounded_by_the_light_ceiling_not_the_heavy_slots():
    world = WorldState(issues=tuple(IssueView(n, labels=frozenset({Label.AI_READY})) for n in (3, 1, 2)))
    chosen = fix_actions(derive(world, local(), H), H, slots=0, light=2)
    assert chosen == (RunAgent(AgentKind.STEWARD, 1), RunAgent(AgentKind.STEWARD, 2))


def test_steward_and_triage_share_the_light_ceiling_and_neither_takes_a_heavy_slot():
    world = WorldState(issues=tuple(IssueView(n, labels=frozenset({Label.AI_READY})) for n in (1, 2, 3)))
    stewarded = {issue_key(2): (LegEnd(AgentKind.STEWARD, T0, False),)}
    states = derive(world, local(legs=stewarded), H)
    assert fix_actions(states, H, slots=0) == (
        RunAgent(AgentKind.STEWARD, 1), RunAgent(AgentKind.TRIAGE, 2), RunAgent(AgentKind.STEWARD, 3))
    assert fix_actions(states, H, slots=0, light=2) == (RunAgent(AgentKind.STEWARD, 1), RunAgent(AgentKind.TRIAGE, 2))


def _slot_world():
    """Issue 5 awaits RESOLVE; PRs 100 and 101 await FIX_CI."""
    return WorldState(
        issues=(IssueView(5, labels=frozenset({Label.AI_READY})),),
        prs=(harness_pr(100, issue=9, ci=ci(bucket="fail")), harness_pr(101, issue=8, ci=ci(bucket="fail"))),
    )


def _heavy(states, **kw):
    return [(a.kind, a.number) for a in fix_actions(states, H, **kw) if isinstance(a, RunAgent)]


def test_a_pr_act_outranks_an_issue_resolve_for_the_one_slot():
    triaged = {issue_key(5): (LegEnd(AgentKind.STEWARD, T0, False), LegEnd(AgentKind.TRIAGE, T0, False))}
    states = derive(_slot_world(), local(legs=triaged), H)
    assert _heavy(states, slots=3) == [(AgentKind.RESOLVE, 5), (AgentKind.FIX_CI, 100), (AgentKind.FIX_CI, 101)]
    assert _heavy(states, slots=1) == [(AgentKind.FIX_CI, 100)]
    assert _heavy(states, slots=2) == [(AgentKind.FIX_CI, 100), (AgentKind.FIX_CI, 101)]


def test_the_longer_waiting_pr_beats_the_lower_numbered_one_and_absent_waits_sort_oldest_then_by_number():
    states = derive(_slot_world(), local(), H)
    one = lambda **kw: [n for k, n in _heavy(states, slots=1, **kw) if k is AgentKind.FIX_CI]
    k = lambda n: pr_key(n)
    assert one(waiting_since={k(100): T0 + timedelta(hours=2), k(101): T0}) == [101]
    assert one(waiting_since={k(100): T0, k(101): T0}) == [100]  # tie: number
    assert one(waiting_since={k(100): T0}) == [101]  # never served is oldest
    assert one() == [100]  # nothing known: by number


def test_1125_starved_behind_1039_1117_1122():
    """Live incident: the high-numbered FIX_CI PR waited behind a newly admitted
    issue's RESOLVE and behind lower-numbered PRs each time they cycled."""
    world = WorldState(
        issues=(IssueView(1039, labels=frozenset({Label.AI_READY})),),
        prs=tuple(harness_pr(n, issue=n - 100, ci=ci(bucket="fail")) for n in (1117, 1122, 1125)),
    )
    states = derive(world, local(), H)
    waiting = {pr_key(1117): T0 + timedelta(minutes=50), pr_key(1122): T0 + timedelta(minutes=40),
               pr_key(1125): T0}
    first = [(k, n) for k, n in _heavy(states, slots=1, waiting_since=waiting) if k not in (AgentKind.STEWARD, AgentKind.TRIAGE)]
    assert first == [(AgentKind.FIX_CI, 1125)]


@pytest.mark.parametrize("slots", [-1, 0])
def test_no_slots_means_no_heavy_dispatch(slots):
    world = WorldState(prs=(harness_pr(100, issue=9, ci=ci(bucket="fail")),))
    assert fix_actions(derive(world, local(), H), H, slots=slots) == ()


# ── reply and question text ──────────────────────────────────────────────────


def test_the_reply_is_the_first_human_comment_after_the_leg():
    after = T0 + timedelta(minutes=5)
    comments = (
        Comment("alice", "too early", (T0 + timedelta(minutes=4)).isoformat()),
        Comment("bob", "second", (T0 + timedelta(minutes=7)).isoformat()),
        Comment("alice", "first", (T0 + timedelta(minutes=6)).isoformat()),
        Comment("fix-bot", f"{H.marker} harness note", (T0 + timedelta(minutes=8)).isoformat()),
        Comment("ci[bot]", "coverage", (T0 + timedelta(minutes=9)).isoformat()),
        Comment("carol", "undated", "yesterday"),
    )
    assert human_reply(comments, after, H, ()) == comments[2]
    assert human_reply(comments[:1] + comments[3:], after, H, ()) is None  # no human answered yet


def test_342_the_reply_skips_comments_inside_any_of_the_items_leg_windows():
    after = T0 + timedelta(minutes=5)
    capped = LegEnd(AgentKind.FIX_CI, T0 + timedelta(minutes=10), True, started_at=T0 + timedelta(minutes=6))
    comments = (
        Comment("fix-bot", "answer", (T0 + timedelta(minutes=5, seconds=30)).isoformat()),
        Comment("fix-bot", "agent progress", (T0 + timedelta(minutes=8)).isoformat()),
        Comment("fix-bot", "follow-up", (T0 + timedelta(minutes=11)).isoformat()),
    )
    assert human_reply(comments, after, H, (capped,)) == comments[0]
    assert human_reply(comments[1:], after, H, (capped,)) == comments[2]  # the mid-leg comment is the agent's


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
    facts = Facts(
        _Control(),
        "o/r",
        active=lambda: (),
        headroom=lambda: True,
        lifetime_capped=unreadable,
        log=lines.append,
        now=lambda: T0,
    )
    loc = facts(WorldState(issues=(IssueView(5, labels=frozenset({Label.AI_READY})),)))
    assert loc.headroom is False and loc.lifetime_capped == frozenset()
    assert len(lines) == 1 and "spend snapshot unavailable" in lines[0]
