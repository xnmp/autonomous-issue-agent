"""#255 at the workflow seam: a leg that declared `blocked` over its own pending
background child is a machine failure, not an attempt, and is never parked
`ai-blocked` for a human (#253's gap)."""

import asyncio

from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.world import Comment, IssueView

from test_dispatch import FakeGateway, FakeRunner, FakeStore
from test_fix_workflow import _resolve_snapshot, _workflow
from test_infra_class_counters import ISSUE, run_resolves


def pending_child_leg():
    """What the runner emits when `blocked` was declared over a live child."""
    return AgentResult(
        is_error=True,
        status="blocked",
        needs="Resume the leg and wait for the adversarial reviewer's PASS",
        session_id="sess-bg",
        cost_usd=2.30,
        exit_reason=ExitReason.BACKGROUND_CHILD_PENDING,
    )


def test_resolve_leg_over_pending_child_is_not_an_attempt_and_never_parks_a_human():
    store = FakeStore()
    gateway = FakeGateway(
        issues={ISSUE: IssueView(ISSUE, title="t", body="b")}, pr_for_issue={}
    )

    run_resolves(store, pending_child_leg(), 5, gateway=gateway)

    assert _resolve_snapshot(store, ISSUE).episode.failures == 0
    assert not any(
        call[:2] == ("relabel", "issue") and call[-1] == "ai-blocked"
        for call in gateway.calls
    )


def test_resume_over_pending_child_returns_issue_to_ready_not_blocked():
    iss = IssueView(5, title="t", body="b", comments=(
        Comment("fix-bot", "asking", "2026-10-01T00:00:01Z"),
        Comment("alice", "proceed, run the review blocking", "2026-10-01T00:00:02Z"),
    ))
    gw = FakeGateway(issues={5: iss}, pr_for_issue={})
    store = FakeStore()
    runner = FakeRunner(pending_child_leg())

    asyncio.run(_workflow(gw, store, runner, fix_bot="fix-bot").resume(5))

    assert runner.tasks
    assert ("relabel", "issue", 5, "ai-working", "ai-ready") in gw.calls
    assert ("relabel", "issue", 5, "ai-working", "ai-blocked") not in gw.calls
