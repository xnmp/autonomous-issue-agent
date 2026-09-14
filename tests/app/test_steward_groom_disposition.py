import asyncio
from dataclasses import replace

from issue_agent.adapters.journal import read_events
from issue_agent.app.planners.schemas import GROOM_SCHEMA
from issue_agent.app.planners.tasks import GroomTaskConfig, TriageTaskConfig
from issue_agent.app.workflows.steward import StewardWorkflow, StewardWorkflowConfig
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.triage import TriageConfig
from issue_agent.domain.world import IssueView
from tests.fakes.executor import ImmediateExecutor as Executor
from tests.fakes.legacy_journal import Journal

from test_dispatch import FakeGateway, FakeRunner, FakeStore, FakeWorktrees


def _workflow(tmp_path, referenced: IssueView):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    current = IssueView(
        196,
        title="Crash in report view",
        body="Opening a report crashes",
        author="alice",
        labels=frozenset({Label.AI_TRIAGE}),
        updated_at="before",
    )
    gateway = FakeGateway(issues={196: current, referenced.number: referenced})
    runner = FakeRunner(AgentResult(
        is_error=False,
        structured={
            "disposition": {
                "kind": "duplicate_of",
                "issue": referenced.number,
                "reason": "The canonical report covers the same crash.",
            }
        },
    ))
    cfg = StewardWorkflowConfig(
        triage=TriageTaskConfig("steward-bot"),
        triage_cfg=TriageConfig("steward-bot", enabled=True, promote=True),
        groom=GroomTaskConfig("steward-bot"),
        log=lambda _message: None,
        repo="o/r",
        journal=journal,
    )
    workflow = StewardWorkflow(
        cfg,
        gateway,
        FakeStore(),
        runner,
        Executor(gateway, remove_worktree=FakeWorktrees().remove),
    )
    return workflow, gateway, runner, journal


def test_groom_duplicate_claim_is_validated_closed_and_journalled(tmp_path):
    workflow, gateway, runner, journal = _workflow(
        tmp_path,
        IssueView(42, state="closed", labels=frozenset({Label.AI_DONE})),
    )

    asyncio.run(workflow.groom(196))

    close = next(call for call in gateway.calls if call[0] == "close")
    assert close[1:3] == (196, "completed")
    assert any(call[0] == "comment" and "validated that #42 exists and is closed" in call[3]
               for call in gateway.calls)
    assert Label.AI_CLOSED in gateway.issues[196].labels
    assert Label.AI_TRIAGE not in gateway.issues[196].labels
    event = next(
        event for event in read_events(journal.path)
        if event.get("event") == "groomed" and event.get("kind") == "duplicate_of"
    )
    assert event["duplicate_of"] == 42
    assert event["outcome"] == "accepted"
    assert "independently verify" in runner.tasks[0].prompt


def test_groom_open_duplicate_target_does_not_close_and_records_refusal(tmp_path):
    workflow, gateway, _runner, journal = _workflow(
        tmp_path, IssueView(42, state="open")
    )

    asyncio.run(workflow.groom(196))

    assert not any(call[0] == "close" for call in gateway.calls)
    event = next(
        event for event in read_events(journal.path)
        if event.get("event") == "groomed" and event.get("kind") == "duplicate_of"
    )
    assert event["outcome"] == "reference_open"


def test_groom_old_closed_non_harness_target_is_refused(tmp_path):
    workflow, gateway, _runner, journal = _workflow(
        tmp_path, IssueView(42, state="closed")
    )

    asyncio.run(workflow.groom(196))

    assert not any(call[0] == "close" for call in gateway.calls)
    event = next(
        event for event in read_events(journal.path)
        if event.get("event") == "groomed" and event.get("kind") == "duplicate_of"
    )
    assert event["outcome"] == "reference_not_harness_done"


def test_groom_active_current_issue_is_refused(tmp_path):
    workflow, gateway, _runner, journal = _workflow(
        tmp_path,
        IssueView(42, state="closed", labels=frozenset({Label.AI_DONE})),
    )
    gateway.issues[196] = replace(
        gateway.issues[196],
        labels=frozenset({Label.AI_TRIAGE, Label.AI_WORKING}),
    )

    asyncio.run(workflow.groom(196))

    assert not any(call[0] == "close" for call in gateway.calls)
    event = next(
        event for event in read_events(journal.path)
        if event.get("event") == "groomed" and event.get("kind") == "duplicate_of"
    )
    assert event["outcome"] == "lifecycle_active"


def test_groom_revalidates_both_issues_before_close(tmp_path):
    done = IssueView(42, state="closed", labels=frozenset({Label.AI_DONE}))
    workflow, gateway, _runner, journal = _workflow(tmp_path, done)
    first_current = gateway.issues[196]
    fresh_current = replace(first_current, state_reason="reopened")
    reads = {
        196: [first_current, fresh_current],
        42: [done, done],
    }

    def sequenced_get_issue(number: int) -> IssueView:
        views = reads[number]
        return views.pop(0) if len(views) > 1 else views[0]

    gateway.get_issue = sequenced_get_issue

    asyncio.run(workflow.groom(196))

    assert not any(call[0] == "close" for call in gateway.calls)
    event = next(
        event for event in read_events(journal.path)
        if event.get("event") == "groomed" and event.get("kind") == "duplicate_of"
    )
    assert event["outcome"] == "reopened"


def test_groom_revalidation_catches_target_reopened_during_paid_run(tmp_path):
    done = IssueView(42, state="closed", labels=frozenset({Label.AI_DONE}))
    workflow, gateway, _runner, journal = _workflow(tmp_path, done)
    current = gateway.issues[196]
    reopened_target = replace(done, state="open", state_reason="reopened")
    reads = {
        196: [current, current],
        42: [done, reopened_target],
    }

    def sequenced_get_issue(number: int) -> IssueView:
        views = reads[number]
        return views.pop(0) if len(views) > 1 else views[0]

    gateway.get_issue = sequenced_get_issue

    asyncio.run(workflow.groom(196))

    assert not any(call[0] == "close" for call in gateway.calls)
    event = next(
        event for event in read_events(journal.path)
        if event.get("event") == "groomed" and event.get("kind") == "duplicate_of"
    )
    assert event["outcome"] == "reference_open"


def test_groom_journals_acceptance_before_a_failing_action_batch(tmp_path):
    workflow, gateway, _runner, journal = _workflow(
        tmp_path,
        IssueView(42, state="closed", labels=frozenset({Label.AI_DONE})),
    )
    gateway.fail_on.add("relabel")

    asyncio.run(workflow.groom(196))

    event = next(
        event for event in read_events(journal.path)
        if event.get("event") == "groomed" and event.get("kind") == "duplicate_of"
    )
    assert event["outcome"] == "accepted"
    assert not any(call[0] == "close" for call in gateway.calls)


def test_groom_journal_failure_does_not_block_close(tmp_path):
    workflow, gateway, _runner, _journal = _workflow(
        tmp_path,
        IssueView(42, state="closed", labels=frozenset({Label.AI_DONE})),
    )
    logs = []

    class ExplodingJournal:
        def emit(self, _event):
            raise OSError("disk full")

    workflow.cfg = replace(workflow.cfg, journal=ExplodingJournal(), log=logs.append)

    asyncio.run(workflow.groom(196))

    assert any(call[0] == "close" for call in gateway.calls)
    assert "groom #196: duplicate disposition not journalled (disk full)" in logs


def test_groom_schema_exposes_only_the_validatable_duplicate_shape():
    disposition = GROOM_SCHEMA["properties"]["disposition"]

    assert disposition["properties"]["kind"]["enum"] == ["duplicate_of"]
    assert disposition["required"] == ["kind", "issue", "reason"]
    assert disposition["additionalProperties"] is False
