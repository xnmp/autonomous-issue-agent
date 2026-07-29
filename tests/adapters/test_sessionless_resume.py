"""Regression coverage for reply-driven recovery without a runtime session."""

import asyncio

from builders import Comment, issue, pr, world
from test_convergence import Scenario, simulate
from test_dispatch import FakeGateway, FakeRunner, FakeStore, SeqRunner
from test_fix_workflow import _workflow

from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
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


def test_sessionless_reply_keeps_world_order_when_saved_session_also_waits():
    blocked = world(
        issues=[
            issue(
                7,
                "ai-blocked",
                comments=(Comment("alice", "first reply", "t1"),),
            ),
            issue(
                8,
                "ai-blocked",
                comments=(Comment("bob", "second reply", "t2"),),
            ),
        ]
    )
    state = FixState(sessions=frozenset({8}))

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
            WorldComment("fix-bot", "Which backend?", "t1"),
            WorldComment("alice", "Use PostgreSQL.", "t2"),
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

    asyncio.run(
        _workflow(
            gateway,
            store,
            runner,
            fix_bot="fix-bot",
        ).resume(5)
    )

    assert runner.tasks[0].resume == "prior-session"


def test_split_author_reply_stays_in_author_phase():
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
    store = FakeStore()
    store.save_session(5, "blocked-author-session")
    runner = FakeRunner(
        AgentResult(is_error=False, status="blocked", session_id="new-author-session")
    )

    asyncio.run(
        _workflow(
            gateway,
            store,
            runner,
            fix_bot="fix-bot",
            author_tests_split=True,
        ).resume(5)
    )

    task = runner.tasks[0]
    assert task.resume is None
    assert "TEST/INTERFACE AUTHOR" in task.prompt
    assert "Use PostgreSQL." in task.prompt
    assert "Do NOT write the implementation" in task.prompt
    assert store.get_phase(5) == ""


def test_split_implementer_reply_stays_in_implementer_phase():
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
    store = FakeStore()
    store.set_phase(5, "authored")
    store.save_session(5, "blocked-implementer-session")
    runner = FakeRunner(
        AgentResult(
            is_error=False,
            status="blocked",
            session_id="new-implementer-session",
        )
    )

    asyncio.run(
        _workflow(
            gateway,
            store,
            runner,
            fix_bot="fix-bot",
            author_tests_split=True,
        ).resume(5)
    )

    task = runner.tasks[0]
    assert task.resume is None
    assert "You are the IMPLEMENTER" in task.prompt
    assert "Use PostgreSQL." in task.prompt
    assert "HARD RULE: do NOT modify" in task.prompt
    assert store.get_phase(5) == "authored"


def test_split_reply_recovery_does_not_rerun_overlap_routing():
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
    gateway.build_world = lambda: world(
        prs=[
            pr(
                100,
                changed_files=frozenset({"src/action.rs"}),
            )
        ]
    )
    runner = FakeRunner(
        AgentResult(
            is_error=False,
            status="blocked",
            session_id="author-session",
            structured={
                "tier": "standard",
                "predicted_paths": ["src/action.rs"],
            },
        )
    )

    asyncio.run(
        _workflow(
            gateway,
            FakeStore(),
            runner,
            fix_bot="fix-bot",
            author_tests_split=True,
            fix=FixTaskConfig("o/r", agent_md="AM"),
        ).resume(5)
    )

    task = runner.tasks[0]
    assert "TEST/INTERFACE AUTHOR" in task.prompt
    assert "Use PostgreSQL." in task.prompt
    assert not any(call[-1] == "ai-ready" for call in gateway.calls)


def test_split_reply_reaches_contract_adjudicator():
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
    store = FakeStore()
    store.set_phase(5, "authored")
    runner = SeqRunner(
        [
            AgentResult(
                is_error=False,
                status="contract_change",
                needs="The reply changes the contract.",
                session_id="implementer-session",
            ),
            AgentResult(
                is_error=False,
                session_id="adjudicator-session",
                structured={"verdict": "upheld", "rationale": "The reply is clear."},
            ),
            AgentResult(
                is_error=False,
                status="blocked",
                session_id="second-implementer-session",
            ),
        ]
    )

    asyncio.run(
        _workflow(
            gateway,
            store,
            runner,
            fix_bot="fix-bot",
            author_tests_split=True,
        ).resume(5)
    )

    assert "adjudicating a contract dispute" in runner.tasks[1].prompt
    assert "Use PostgreSQL." in runner.tasks[1].prompt
    assert "Use PostgreSQL." in runner.tasks[2].prompt
