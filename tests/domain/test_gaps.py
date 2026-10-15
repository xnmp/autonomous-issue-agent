"""Harness gaps the harness files about itself: an agent's `harness_issue`
report and a machine-end stall, filed into the harness's own repository with
harness facts only (no agent text), never for a judgment stop, and never
picked up by itself."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain import world as world_module
from issue_agent.domain.actions import AgentKind, ReportGap
from issue_agent.domain.backoff import STALL_STREAK, LegEnd
from issue_agent.domain.gaps import MAX_GAP_BODY, MAX_GAP_TITLE, GapTarget, HarnessGap, parse_harness_gap
from issue_agent.domain.labels import Label
from issue_agent.domain.policy import gap_actions
from issue_agent.domain.state import (
    HARNESS_GAP_PREFIX, Harness, Local, QuestionStop, derive, issue_key, pr_key,
)
from issue_agent.domain.world import IssueView, WorldState

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
HARNESS_REPO = "xnmp/autonomous-issue-agent"
MARKER = "<!-- issue-agent -->"
H = Harness("fix-bot", "review-bot")
PUBLIC, PRIVATE = GapTarget("o/tauri", True), GapTarget("o/ballast", False)
AGENT_TEXT = "SECRET-AGENT-PROSE @owner <img src=x> ghp_" + "a" * 36


def at(minute: float) -> datetime:
    return T0 + timedelta(minutes=minute)


def reported(minute: float, kind: AgentKind = AgentKind.RESOLVE, question: str | None = None,
             gap: bool = True, leg_id: str = "leg-1") -> LegEnd:
    return LegEnd(kind, at(minute), False, question=question, harness_gap=gap, leg_id=leg_id, runtime="claude")


def machine(kind: AgentKind, *minutes: float, end: str = "transport_death") -> tuple[LegEnd, ...]:
    return tuple(LegEnd(kind, at(m), True, leg_id=f"m{m}", runtime="codex", end=end) for m in minutes)


def gaps(legs, live=(), target=PUBLIC, harness_repo=HARNESS_REPO, reported_keys=frozenset()):
    found = gap_actions(legs, live, target, harness_repo, MARKER, reported_keys)
    assert all(isinstance(a, ReportGap) for a in found)  # never a label, a comment or a dispatch on the target
    return found


# ── the agent's claim: bounded, kept on the host ────────────────────────────


@pytest.mark.parametrize("raw", [
    None, "a title", ["t", "b"], {}, {"body": "no title"}, {"title": ""}, {"title": " \n\t "}, {"title": 7},
    {"title": "t", "body": 7}, {"title": "t" * (MAX_GAP_TITLE + 1)}, {"title": "t", "body": "b" * (MAX_GAP_BODY + 1)},
    {"title": "t" * 10_000_000},
])
def test_a_malformed_absent_or_oversized_report_is_no_report(raw):
    assert parse_harness_gap(raw) is None


def test_a_report_at_its_bounds_is_kept_and_round_trips_through_its_record():
    gap = parse_harness_gap({"title": " Sandbox\n lacks  glib " + "t" * (MAX_GAP_TITLE - 22), "body": "b" * MAX_GAP_BODY})
    assert gap is not None and "\n" not in gap.title and parse_harness_gap(gap.record()) == gap
    assert parse_harness_gap({"title": "t"}) == HarnessGap("t", "")


def test_only_an_agent_end_reports_a_gap_and_only_a_machine_end_has_an_end_class():
    with pytest.raises(ValueError):
        LegEnd(AgentKind.RESOLVE, T0, True, harness_gap=True)
    with pytest.raises(ValueError):
        LegEnd(AgentKind.RESOLVE, T0, False, end="transport_death")
    with pytest.raises(ValueError):
        LegEnd(AgentKind.RESOLVE, T0, True, end="Traceback: /home/op/secret.py line 3")  # never a raw error


# ── an agent's report: harness facts only ───────────────────────────────────


def test_an_agent_report_files_one_issue_of_harness_facts_naming_and_linking_a_public_target():
    (filed,) = gaps({issue_key(5): (reported(1), reported(2, leg_id="leg-2"))})  # twice on one item: one report
    assert (filed.repo, filed.title) == (HARNESS_REPO, "Harness gap: agent-reported harness defect in o/tauri")
    assert filed.body.startswith(MARKER)
    for fact in ("`o/tauri#5` (issue)", "https://github.com/o/tauri/issues/5", "RESOLVE leg `leg-1`", "(claude)",
                 "leg receipt `leg-1`"):
        assert fact in filed.body and fact in filed.recurrence


def test_a_private_target_is_never_named_hashed_or_linked_but_keeps_its_own_dedup_key():
    (filed,) = gaps({pr_key(9): (reported(1, AgentKind.FIX_CI),)}, target=PRIVATE)
    published = filed.title + filed.body + filed.recurrence
    assert "ballast" not in published.lower() and "github.com" not in published
    assert "a private target" in filed.title
    (other,) = gaps({pr_key(9): (reported(1, AgentKind.FIX_CI),)}, target=GapTarget("o/vault", False))
    assert other.title == filed.title  # private targets may share one public issue...
    assert other.key != filed.key  # ...yet each item reports once, deduplicated locally


def test_items_hitting_the_same_gap_share_its_issue_with_one_report_each():
    filed = gaps({issue_key(5): (reported(1),), pr_key(9): (reported(2, AgentKind.FIX_CI),),
                  issue_key(6): (reported(3),)})
    assert len({a.key for a in filed}) == 3 and len({a.title for a in filed}) == 1


def test_a_report_already_made_is_never_asked_for_again():
    legs = {issue_key(5): (reported(1),), issue_key(6): machine(AgentKind.RESOLVE, *range(STALL_STREAK))}
    made = frozenset(a.key for a in gaps(legs, [issue_key(6)]))
    assert len(made) == 2 and gaps(legs, [issue_key(6)], reported_keys=made) == ()


def test_nothing_is_filed_when_the_harness_repo_is_unknown():
    assert gaps({issue_key(5): (reported(1),)}, [issue_key(5)], harness_repo="") == ()


# ── judgment stops are the human's ──────────────────────────────────────────


def test_a_judgment_stop_is_never_filed():
    legs = {issue_key(5): (reported(1, question="Which locale is canonical?", gap=False),),
            pr_key(9): tuple(reported(m, AgentKind.FIX_CI, gap=False) for m in (1, 2, 3)),
            pr_key(10): tuple(reported(m, AgentKind.ADDRESS_REVIEW, gap=False) for m in (1, 2, 3))}
    assert gaps(legs, list(legs)) == ()


def test_a_question_with_a_report_files_the_report_and_still_stops_the_item_for_the_human():
    leg = reported(1, question="Release this once the sandbox has libglib")
    (filed,) = gaps({issue_key(5): (leg,)})
    assert "Release this once" not in filed.body + filed.recurrence
    world = WorldState(issues=(IssueView(5, labels=frozenset({Label.AI_READY})),))
    (state,) = derive(world, Local(now=at(10), legs={issue_key(5): (leg,)}), H).values()
    assert state == QuestionStop(leg.question, leg.ended_at.isoformat())


# ── a stall the harness observes ────────────────────────────────────────────


def test_a_machine_stall_on_a_live_item_is_filed_once_its_backoff_reaches_the_cap():
    item = issue_key(5)
    assert gaps({item: machine(AgentKind.RESOLVE, *range(STALL_STREAK - 1))}, [item]) == ()
    (filed,) = gaps({item: machine(AgentKind.RESOLVE, *range(STALL_STREAK))}, [item])
    assert filed.title == f"Harness gap: RESOLVE: {STALL_STREAK} consecutive machine ends in o/tauri"
    assert "machine end `transport_death`" in filed.body and "(codex)" in filed.body
    (longer,) = gaps({item: machine(AgentKind.RESOLVE, *range(STALL_STREAK + 3))}, [item])
    assert longer == filed  # stable while the streak grows, so a lost reply is recognised


@pytest.mark.parametrize("outside", ["operator_cancelled", "provider_refused"])
def test_teardowns_and_provider_windows_never_make_a_stall(outside):
    item = issue_key(5)
    ends = machine(AgentKind.RESOLVE, *range(STALL_STREAK), end=outside)
    assert gaps({item: ends}, [item]) == ()
    mixed = ends + machine(AgentKind.RESOLVE, *range(100, 100 + STALL_STREAK - 1))
    assert gaps({item: mixed}, [item]) == ()  # they neither count nor break the harness's own streak
    (filed,) = gaps({item: mixed + machine(AgentKind.RESOLVE, 200)}, [item])
    assert "RESOLVE" in filed.title


def test_an_agent_end_breaks_the_stall_and_a_stall_off_the_live_items_is_not_filed():
    item = issue_key(5)
    assert gaps({item: (*machine(AgentKind.RESOLVE, *range(STALL_STREAK)), reported(100, gap=False))}, [item]) == ()
    assert gaps({item: machine(AgentKind.RESOLVE, *range(STALL_STREAK))}, []) == ()


# ── never picked up by itself ───────────────────────────────────────────────


def _modes():
    """Every issue-pickup mode the world knows: the default (`ai-ready` only),
    and any automatic mode a later change adds (`world.Pickup`)."""
    pickup = getattr(world_module, "Pickup", None)
    return [{}] + [{"pickup": mode} for mode in (pickup or ())]


def test_a_filed_gap_is_never_picked_up_automatically_in_any_mode_but_a_humans_ai_ready_is_honoured():
    (filed,) = gaps({issue_key(5): (reported(1),)})
    assert filed.title.startswith(HARNESS_GAP_PREFIX)
    gap = IssueView(41, filed.title, filed.body, author="fix-bot")
    for mode in _modes():
        assert derive(WorldState(issues=(gap,), **mode), Local(now=at(10)), H) == {}, mode
    readied = IssueView(41, filed.title, filed.body, labels=frozenset({Label.AI_READY}), author="fix-bot")
    for mode in _modes():
        assert set(derive(WorldState(issues=(readied,), **mode), Local(now=at(10)), H)) == {issue_key(41)}, mode

