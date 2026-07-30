"""Regression coverage for executor-owned human-wait journal spans."""

from issue_agent.app.execute import Executor
from issue_agent.app.roles import _hold_reporter
from issue_agent.domain.actions import Relabel, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.world import IssueView, WorldState

from test_journal import FakeGateway
from test_roles import _holds_in
from issue_agent.adapters.journal import Journal, read_events


def test_issue_block_and_reply_resume_journal_one_human_wait_span(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    executor = Executor(FakeGateway(), journal=journal)

    executor.execute(Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_BLOCKED))
    executor.execute(Relabel(Target.ISSUE, 5, Label.AI_BLOCKED, Label.AI_WORKING))

    waits = [event for event in read_events(journal.path) if event["event"] == "deferred"]
    assert [(event["state"], event["reason"], event["blocker"]) for event in waits] == [
        ("waiting", "human", "human"),
        ("cleared", "human", "human"),
    ]


def test_pr_block_does_not_create_an_issue_human_wait(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    executor = Executor(FakeGateway(), journal=journal)

    executor.execute(Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.AI_BLOCKED))

    assert [event for event in read_events(journal.path) if event["event"] == "deferred"] == []


def test_restart_reconciles_human_wait_after_manual_unblock(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    journal.emit(
        "deferred",
        state="waiting",
        issue=5,
        reason="human",
        blocker="human",
    )

    blocked = WorldState(issues=(
        IssueView(5, labels=frozenset({Label.AI_BLOCKED})),
    ))
    ready = WorldState(issues=(
        IssueView(5, labels=frozenset({Label.AI_READY})),
    ))
    _hold_reporter(journal, "o/r", lambda _message: None)(blocked)
    _hold_reporter(journal, "o/r", lambda _message: None)(ready)

    human_events = [
        event
        for event in _holds_in(journal)
        if event.get("reason") == "human"
    ]
    assert [event["state"] for event in human_events] == ["waiting", "cleared"]
    assert human_events[-1]["blocker"] == "human"
