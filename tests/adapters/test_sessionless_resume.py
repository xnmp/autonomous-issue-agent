"""Regression coverage for reply-driven recovery without a runtime session."""

import asyncio

from builders import Comment, issue, world
from test_convergence import Scenario, simulate
from test_dispatch import FakeGateway, FakeRunner, FakeStore
from test_fix_workflow import _workflow

from issue_agent.domain.actions import AgentKind
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.world import Comment as WorldComment
from issue_agent.domain.world import IssueView


def test_sessionless_blocked_reply_dispatches_once_as_resume(tmp_path):
    scenario = Scenario(
        name="sessionless-resume-dedup",
        world=world(
            issues=[
                issue(
                    7,
                    "ai-blocked",
                    comments=(
                        Comment(
                            "alice",
                            "here is the missing context",
                            "2026-01-01T00:00:00Z",
                        ),
                    ),
                )
            ]
        ),
        runner_result=AgentResult(
            is_error=False,
            status="blocked",
            needs="still blocked",
            session_id="fresh-session",
        ),
    )

    trace = simulate(scenario, tmp_path)
    dispatches = [run for tick in trace for run in tick.dispatches]

    assert [run.kind for run in dispatches] == [AgentKind.RESUME]


def test_sessionless_reply_starts_fresh_with_issue_and_reply_context():
    iss = IssueView(
        5,
        title="Choose storage",
        body="The service must persist widgets.",
        comments=(
            WorldComment("fix-bot", "Which backend?", "t1"),
            WorldComment("alice", "Use PostgreSQL.", "t2"),
        ),
    )
    gateway = FakeGateway(issues={5: iss}, pr_for_issue={})
    runner = FakeRunner(
        AgentResult(is_error=False, status="blocked", session_id="new-session")
    )

    asyncio.run(
        _workflow(gateway, FakeStore(), runner, fix_bot="fix-bot").resume(5)
    )

    task = runner.tasks[0]
    assert task.resume == ""
    assert "Choose storage" in task.prompt
    assert "The service must persist widgets." in task.prompt
    assert "Use PostgreSQL." in task.prompt


def test_reply_with_session_continues_the_saved_runtime_handle():
    iss = IssueView(
        5,
        title="t",
        body="b",
        comments=(
            WorldComment("fix-bot", "question", "t1"),
            WorldComment("alice", "answer", "t2"),
        ),
    )
    gateway = FakeGateway(issues={5: iss}, pr_for_issue={})
    store = FakeStore()
    store.save_session(5, "prior-session")
    runner = FakeRunner(
        AgentResult(
            is_error=False,
            status="blocked",
            session_id="prior-session",
        )
    )

    asyncio.run(_workflow(gateway, store, runner, fix_bot="fix-bot").resume(5))

    assert runner.tasks[0].resume == "prior-session"
