"""Regression coverage for reply-driven recovery without a runtime session."""

import asyncio


from tests.domain.builders import Comment, issue, world
from test_convergence import Scenario, simulate, controlled_publication_context  # noqa: F401 -- autouse fixture
from test_dispatch import FakeGateway, FakeRunner, FakeStore
from test_fix_workflow import _workflow

from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.world import Comment as WorldComment
from issue_agent.domain.world import IssueView
from tests.fakes.accounted_workflow import seed_resolve, resolve_state, attach_workflow_authority
from tests.fakes.workflow_authority import resolve_snapshot, with_issue_recovery


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


def test_sessionless_reply_keeps_world_order_when_saved_session_also_waits():
    blocked = world(
        issues=[
            issue(
                7,
                "ai-blocked",
                comments=(Comment("alice", "first reply", "2026-10-01T00:00:00Z"),),
            ),
            issue(
                8,
                "ai-blocked",
                comments=(Comment("bob", "second reply", "2026-10-02T00:00:00Z"),),
            ),
        ]
    )
    store = FakeStore()
    seed_resolve(store, 8, session_id="saved")
    attach_workflow_authority(FakeGateway(issues={8: blocked.issues[1]}), store)
    state = FixState(resolve_workflows={7: with_issue_recovery(resolve_snapshot(7)), 8: resolve_state(store, 8)})

    actions = decide_fix(
        blocked,
        FixConfig(fix_bot="fix-bot"),
        state,
        fixer_busy=False,
    )
    dispatches = [action for action in actions if isinstance(action, RunAgent)]

    assert [(run.kind, run.number) for run in dispatches] == [
        (AgentKind.RESUME, 7)
    ]


def test_sessionless_reply_starts_fresh_with_issue_and_reply_context():
    iss = IssueView(
        5,
        title="Choose storage",
        body="The service must persist widgets.",
        comments=(
            WorldComment("fix-bot", "Which backend?", "2026-10-01T00:00:00Z"),
            WorldComment("alice", "Use PostgreSQL.", "2026-10-02T00:00:00Z"),
        ),
    )
    gateway = FakeGateway(issues={5: iss}, pr_for_issue={})
    runner = FakeRunner(
        AgentResult(is_error=False, status="blocked", session_id="new-session")
    )

    asyncio.run(
        _workflow(
            gateway,
            FakeStore(),
            runner,
            fix_bot="fix-bot",
        ).resume(5)
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
            WorldComment("fix-bot", "question", "2026-10-01T00:00:00Z"),
            WorldComment("alice", "answer", "2026-10-02T00:00:00Z"),
        ),
    )
    gateway = FakeGateway(issues={5: iss}, pr_for_issue={})
    store = FakeStore()
    seed_resolve(store, 5, session_id="prior-session")
    runner = FakeRunner(
        AgentResult(
            is_error=False,
            status="blocked",
            session_id="prior-session",
        )
    )

    asyncio.run(
        _workflow(
            gateway,
            store,
            runner,
            fix_bot="fix-bot",
        ).resume(5)
    )

    assert runner.tasks[0].resume == "prior-session"


