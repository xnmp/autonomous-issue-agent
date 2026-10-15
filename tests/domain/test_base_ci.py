"""Gap 22 / #402 (ADR 0017): one base-CI issue per workflow per red streak,
and a required workflow's issue readied and fixed first. Through
`base_ci_actions`, `derive` and `dispatch_order`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from issue_agent.domain.actions import AgentKind, FileIssue, Relabel, SetIssueState, Target
from issue_agent.domain.backoff import LegEnd
from issue_agent.domain.checks import BaseCI, BaseIssue, BaseRun, BaseTip, Check, Observed, RequiredCheck
from issue_agent.domain.evidence import new_failures
from issue_agent.domain.labels import Label
from issue_agent.domain.policy import base_ci_actions, dispatch_order
from issue_agent.domain.state import Dispatch, Harness, Local, Wait, WaitReason, derive, issue_key, pr_key
from issue_agent.domain.world import BlockerRef, IssueView, PRView, WorldState

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
PREFIX = "CI: macOS Smoke failing on dev"
CLOSED_AT = "2026-10-14T02:00:00Z"


def _br(run: int, conclusion: str, name: str = "macOS Smoke", hour: int = 0) -> BaseRun:
    at = f"2026-10-14T{hour:02d}:00:{run:02d}Z"
    return BaseRun(name, run, f"https://x/runs/{run}", f"sha{run}", conclusion, at, at)


def _act(*runs, issues=(), required=None, admitted=()):
    return base_ci_actions(BaseCI(tuple(runs), tuple(issues), required), "dev", "<!-- m -->", admitted)


def _filed(actions) -> list[FileIssue]:
    return [a for a in actions if isinstance(a, FileIssue)]


# ── one issue per red streak ─────────────────────────────────────────────────


def test_an_unread_base_acts_on_nothing():
    assert base_ci_actions(None, "dev", "<!-- m -->") == ()


def test_a_red_streak_files_one_issue_named_and_keyed_by_its_first_failing_run():
    (filed,) = _act(_br(1, "success"), _br(2, "failure"), _br(3, "failure"))
    assert isinstance(filed, FileIssue) and filed.title == f"{PREFIX} since sha2"
    assert filed.key == f"base-ci:dev:{PREFIX} since sha2"
    assert filed.body.startswith("<!-- m -->\n") and "First failing run: https://x/runs/2 (head `sha2`)" in filed.body


def test_a_streak_reaching_past_the_listing_says_so():
    (filed,) = _act(_br(4, "failure"))
    assert "Earliest listed failing run: https://x/runs/4" in filed.body


def test_an_open_issue_of_the_workflow_files_nothing_and_green_closes_it():
    for title in (f"{PREFIX} since sha2", PREFIX):  # a legacy issue counts as the current streak's
        assert _act(_br(2, "failure"), issues=[BaseIssue(40, title)]) == ()
        (closed,) = _act(_br(9, "success"), issues=[BaseIssue(40, title)])
        assert closed == SetIssueState(40, False, "<!-- m -->\n**macOS Smoke** passes on `dev` again: "
                                                  "https://x/runs/9 (head `sha9`).")
    assert _act(_br(9, "success"), issues=[BaseIssue(40, PREFIX, CLOSED_AT)]) == ()


def test_a_new_red_streak_after_green_files_a_fresh_issue():
    issues = [BaseIssue(40, f"{PREFIX} since sha2", CLOSED_AT)]
    (filed,) = _act(_br(2, "failure"), _br(3, "success", hour=1), _br(5, "failure", hour=3), issues=issues)
    assert filed.title == f"{PREFIX} since sha5"


def test_a_close_while_red_files_the_next_issue_on_the_next_red_run_only():
    # Closed at 02:00 (a human, or `Closes #` on a fix's merge) with the streak still red.
    issues = [BaseIssue(40, f"{PREFIX} since sha1", CLOSED_AT)]
    assert _act(_br(1, "failure"), _br(2, "failure", hour=1), issues=issues) == ()
    (filed,) = _act(_br(1, "failure"), _br(2, "failure", hour=1), _br(3, "failure", hour=3), issues=issues)
    assert filed.title == f"{PREFIX} since sha3"


def test_a_title_stays_within_githubs_limit_whatever_the_base_branch():
    base = "release/" + "b" * 192
    (filed,) = base_ci_actions(BaseCI((_br(2, "failure", name="w" * 300),), ()), base, "<!-- m -->")
    assert len(filed.title) <= 256 and f" failing on {base} since " in filed.title  # what the issue search finds


def test_another_bases_or_workflows_issue_is_not_the_workflows():
    others = [BaseIssue(40, "CI: macOS Smoke failing on main"), BaseIssue(41, "CI: macOS Smoke 2 failing on dev")]
    assert _filed(_act(_br(2, "failure"), issues=others))[0].title == f"{PREFIX} since sha2"


def test_a_hostile_or_huge_workflow_name_is_bounded_in_the_title_and_inert_in_the_body():
    name = "@owner <img src=x>\n![p](http://x) " + "w" * 5000
    (filed,) = _act(_br(2, "failure", name=name))
    assert len(filed.title) <= 256 and "\n" not in filed.title and len(filed.body) < 1000
    assert "@owner" not in filed.body and "<img" not in filed.body and "![p]" not in filed.body


# ── a required workflow's issue is readied ───────────────────────────────────

READY = Relabel(Target.ISSUE, 40, remove=None, add=Label.AI_READY)
OPEN = [BaseIssue(40, f"{PREFIX} since sha2")]


def test_a_required_workflows_open_issue_is_readied_unless_already_ready_or_parked():
    assert READY in _act(_br(2, "failure"), issues=OPEN, required=frozenset({"macOS Smoke"}))
    assert _act(_br(2, "failure"), issues=OPEN, required=frozenset({"macOS Smoke"}), admitted=(40,)) == ()


def test_a_non_required_or_unmapped_workflows_issue_is_not_readied():
    for required in (frozenset({"Lint"}), frozenset(), None):
        assert _act(_br(2, "failure"), issues=OPEN, required=required) == ()


# ── base-fix work through derive ─────────────────────────────────────────────

H = Harness("fix-bot", "review-bot")
POLICY = frozenset({RequiredCheck("smoke")})
RED_SMOKE = Check("smoke", "FAILURE", "fail", required=True)
TIP = BaseTip("dev", "b9", (RED_SMOKE,))
LIGHT = (LegEnd(AgentKind.STEWARD, NOW - timedelta(hours=2), False),
         LegEnd(AgentKind.TRIAGE, NOW - timedelta(hours=1), False))


def _world(*, required=frozenset({"macOS Smoke"}), issues=(), prs=()) -> WorldState:
    return WorldState(issues=tuple(issues), prs=tuple(prs), base=TIP, now=NOW.isoformat(),
                      base_ci=BaseCI((_br(2, "failure"),), tuple(OPEN), required))


def _issue(number: int, **kw) -> IssueView:
    return IssueView(number=number, title=f"{PREFIX} since sha2", author="fix-bot",
                     labels=frozenset({Label.AI_READY}), **kw)


def _pr(issue: int) -> PRView:
    return PRView(number=70, head_ref_name=f"fix/{issue}-x", head_ref_oid="h1", base_ref_name="dev",
                  mergeable="MERGEABLE", ci=Observed("h1", "h1", POLICY, (RED_SMOKE,)))


def test_the_base_fix_issue_resolves_at_once_without_steward_triage_or_its_blocker():
    states = derive(_world(issues=[_issue(40, blocked_by=(BlockerRef(5),))]), Local(NOW, {}), H)
    assert states[issue_key(40)] == Dispatch(AgentKind.RESOLVE)


def test_unmapped_it_is_an_ordinary_issue():
    states = derive(_world(required=None, issues=[_issue(40)]), Local(NOW, {}), H)
    assert states[issue_key(40)] == Dispatch(AgentKind.STEWARD)


def test_the_base_fix_pr_is_charged_the_bases_failure_and_another_pr_waits_as_base_red():
    fix = derive(_world(prs=[_pr(40)]), Local(NOW, {issue_key(40): LIGHT}), H)[pr_key(70)]
    assert isinstance(fix, Dispatch) and fix == Dispatch(AgentKind.FIX_CI, "h1", fix.tier,
                                                         new_failures(_pr(40).ci, ()))
    assert derive(_world(prs=[_pr(41)]), Local(NOW, {}), H)[pr_key(70)] == Wait(WaitReason.BASE_RED)


def test_base_fix_work_is_served_first():
    keys = [pr_key(7), issue_key(3), issue_key(40)]
    assert dispatch_order(keys, {}, frozenset({issue_key(40)}))[0] == issue_key(40)
    assert dispatch_order(keys, {})[0] == pr_key(7)
