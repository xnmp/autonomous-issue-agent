"""Steward and Jira workflow wiring tests."""

import asyncio

from tests.fakes.legacy_journal import Journal
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.journal import read_events
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.workflows.jira import JiraSyncWorkflow, JiraSyncWorkflowConfig
from issue_agent.app.workflows.steward import StewardWorkflow, StewardWorkflowConfig
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.app.planners.tasks import GroomTaskConfig, JiraTaskConfig, TriageTaskConfig
from issue_agent.domain.triage import TriageConfig
from issue_agent.domain.world import Comment, IssueView

from test_dispatch import FakeGateway, FakeRunner, FakeStore, FakeWorktrees


def test_steward_triage_promotes_ready_issue_and_records_post_action_updated_at():
    issue = IssueView(
        7,
        title="Export data",
        body="want csv",
        author="alice",
        comments=(Comment("alice", "please"),),
        updated_at="before",
    )
    updated = IssueView(7, title="Export data", body="want csv", author="alice", updated_at="after")

    class UpdatingGateway(FakeGateway):
        def __init__(self):
            super().__init__(issues={7: issue})
            self.reads = 0

        def get_issue(self, number):
            self.reads += 1
            return issue if self.reads == 1 else updated

    gw = UpdatingGateway()
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, structured={
        "verdict": "ready",
        "plain_summary": "clear",
        "acceptance_criteria": ["exports csv"],
        "creator_questions": [],
        "owner_questions": [],
        "blocked_by_issues": [],
        "rationale": "enough detail",
    }))
    cfg = StewardWorkflowConfig(
        triage=TriageTaskConfig("steward-bot", triage_prompt="T"),
        triage_cfg=TriageConfig("steward-bot", enabled=True, promote=True),
        log=lambda _m: None,
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)

    asyncio.run(StewardWorkflow(cfg, gw, store, runner, executor).triage(7))

    assert any(c[0] == "relabel" and c[4] == "ai-ready" for c in gw.calls)
    assert store.triage_at[7] == "after"


def test_steward_triage_blocked_asks_author_and_records_updated_at():
    issue = IssueView(
        7,
        title="Add export",
        body="want it",
        author="alice",
        comments=(Comment("alice", "please add export"),),
        updated_at="t_post",
    )
    gw = FakeGateway(issues={7: issue})
    store = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False,
        structured={
            "verdict": "blocked",
            "plain_summary": "Need detail",
            "creator_questions": ["Which format?"],
            "owner_questions": [],
            "acceptance_criteria": [],
        },
    ))
    cfg = StewardWorkflowConfig(
        triage=TriageTaskConfig("steward-bot", triage_prompt="T"),
        triage_cfg=TriageConfig("steward-bot", enabled=True),
        log=lambda _m: None,
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)

    asyncio.run(StewardWorkflow(cfg, gw, store, runner, executor).triage(7))

    assert any(c[0] == "comment" and "@alice" in c[3] for c in gw.calls)
    assert ("relabel", "issue", 7, None, "ai-triage") in gw.calls
    assert store.triage_at[7] == "t_post"


def test_steward_triage_error_leaves_state_untouched():
    gw = FakeGateway(issues={7: IssueView(7, title="Export data", body="want csv")})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=True, status="error"))
    cfg = StewardWorkflowConfig(
        triage=TriageTaskConfig("steward-bot", triage_prompt="T"),
        triage_cfg=TriageConfig("steward-bot", enabled=True, promote=True),
        log=lambda _m: None,
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)

    asyncio.run(StewardWorkflow(cfg, gw, store, runner, executor).triage(7))

    assert gw.calls == []
    assert store.triage_at == {}


# ── semantic recurrences riding the triage leg (harness #10) ─────────────────
class _Merged:
    def __init__(self, issue, pr, title="Login crashes", merged_at="2026-07-20"):
        self.issue, self.pr = issue, pr
        self.title, self.merged_at = title, merged_at


class RecurrenceGateway(FakeGateway):
    """FakeGateway + the merged-issue reference set the recurrence check reads."""

    def __init__(self, issues, merged=(), raises=False):
        super().__init__(issues=issues)
        self.merged = merged
        self.raises = raises

    def recently_merged_issues(self, author, limit=30):
        if self.raises:
            raise RuntimeError("gh boom")
        return self.merged


def _verdict(**extra):
    return AgentResult(is_error=False, structured={
        "verdict": "ready", "plain_summary": "clear", "acceptance_criteria": ["ac"],
        "creator_questions": [], "owner_questions": [], "blocked_by_issues": [],
        **extra,
    })


def _recurrence_cfg(journal=None):
    return StewardWorkflowConfig(
        triage=TriageTaskConfig("steward-bot", triage_prompt="T"),
        triage_cfg=TriageConfig("steward-bot", enabled=True),
        log=lambda _m: None, fix_bot="fix-bot", repo="o/r", journal=journal,
    )


def _run(gw, cfg, runner, store=None):
    store = store or FakeStore()
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    asyncio.run(StewardWorkflow(cfg, gw, store, runner, executor).triage(9))
    return store


def _recurrences(journal):
    return [e for e in read_events(journal.path) if e.get("event") == "recurrence"]


def test_a_refiled_issue_gets_the_label_a_comment_naming_the_original_and_one_event(tmp_path):
    """AC: a new issue judged to re-report a merged one gets the label + a comment
    naming the original, and journals once."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = RecurrenceGateway({9: IssueView(9, title="Still crashing", body="b", author="alice")},
                           merged=(_Merged(42, 300),))

    _run(gw, _recurrence_cfg(journal), FakeRunner(_verdict(recurrence_of=42)))

    assert ("relabel", "issue", 9, None, "recurrence") in gw.calls
    body = next(c[3] for c in gw.calls if c[0] == "comment" and "recurrence" in c[3])
    assert "#42" in body and "PR #300" in body
    assert _recurrences(journal) == [{
        "ts": _recurrences(journal)[0]["ts"], "event": "recurrence", "kind": "refiled",
        "issue": 9, "of_issue": 42, "pr": 300,
        "block_key": "recurrence:o/r:9:refiled:42",
    }]


def test_the_merged_list_is_rendered_into_the_triage_prompt():
    gw = RecurrenceGateway({9: IssueView(9, title="t", body="b", author="alice")},
                           merged=(_Merged(42, 300),))
    runner = FakeRunner(_verdict())

    _run(gw, _recurrence_cfg(), runner)

    assert "- #42 Login crashes (PR #300, merged 2026-07-20)" in runner.tasks[0].prompt


def test_a_re_triaged_recurrence_journals_only_once(tmp_path):
    """A recurrence is a level (the issue keeps re-entering triage as the author
    replies); the metric counts events, so the second pass must add nothing."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = RecurrenceGateway({9: IssueView(9, title="t", body="b", author="alice")},
                           merged=(_Merged(42, 300),))
    cfg = _recurrence_cfg(journal)
    store, executor = FakeStore(), Executor(gw, remove_worktree=FakeWorktrees().remove)
    wf = StewardWorkflow(cfg, gw, store, FakeRunner(_verdict(recurrence_of=42)), executor)

    asyncio.run(wf.triage(9))
    asyncio.run(wf.triage(9))

    assert len(_recurrences(journal)) == 1


def test_no_recurrence_claimed_leaves_triage_exactly_as_it_was(tmp_path):
    """AC: an unrelated new issue gets neither the label nor a comment about it."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = RecurrenceGateway({9: IssueView(9, title="t", body="b", author="alice")},
                           merged=(_Merged(42, 300),))

    store = _run(gw, _recurrence_cfg(journal), FakeRunner(_verdict()))

    assert not any(c[0] == "relabel" and c[4] == "recurrence" for c in gw.calls)
    assert not any(c[0] == "comment" and "recurrence" in c[3] for c in gw.calls)
    assert _recurrences(journal) == []
    assert store.triage_at[9] == ""          # triage itself still completed


def test_a_recurrence_of_an_issue_the_harness_never_shipped_is_discarded(tmp_path):
    """Containment: the classifier reads attacker-writable issue bodies, so a
    number outside the supplied merged list must reach nothing."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = RecurrenceGateway({9: IssueView(9, title="t", body="b", author="alice")},
                           merged=(_Merged(42, 300),))

    _run(gw, _recurrence_cfg(journal), FakeRunner(_verdict(recurrence_of=999)))

    assert not any(c[0] == "relabel" and c[4] == "recurrence" for c in gw.calls)
    assert _recurrences(journal) == []


def test_a_non_integer_recurrence_of_is_ignored(tmp_path):
    gw = RecurrenceGateway({9: IssueView(9, title="t", body="b", author="alice")},
                           merged=(_Merged(42, 300),))

    _run(gw, _recurrence_cfg(), FakeRunner(_verdict(recurrence_of="42")))

    assert not any(c[0] == "relabel" and c[4] == "recurrence" for c in gw.calls)


def test_a_failing_merged_list_fetch_still_triages(tmp_path):
    """The recurrence check must never break triage: no reference set, no
    recurrence section — and the verdict is still applied and recorded."""
    gw = RecurrenceGateway({9: IssueView(9, title="t", body="b", author="alice",
                                         updated_at="t_post")},
                           merged=(), raises=True)
    runner = FakeRunner(_verdict(recurrence_of=42))

    store = _run(gw, _recurrence_cfg(), runner)

    assert ("relabel", "issue", 9, None, "ai-triage") in gw.calls
    assert store.triage_at[9] == "t_post"
    assert "recurrence check" not in runner.tasks[0].prompt


def test_a_gateway_without_the_merged_read_triages_unchanged():
    """FakeGateway has no `recently_merged_issues` — a legacy adapter must not
    turn the triage leg into an AttributeError."""
    gw = FakeGateway(issues={9: IssueView(9, title="t", body="b", author="alice")})
    runner = FakeRunner(_verdict(recurrence_of=42))

    _run(gw, _recurrence_cfg(), runner)

    assert ("relabel", "issue", 9, None, "ai-triage") in gw.calls
    assert not any(c[0] == "relabel" and c[4] == "recurrence" for c in gw.calls)


def test_grooming_flags_a_resolved_open_issue_once_and_never_closes_it(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = RecurrenceGateway({9: IssueView(9, title="Old crash", body="b", author="alice", updated_at="after")},
                           merged=(_Merged(42, 300),))
    cfg = StewardWorkflowConfig(
        triage=TriageTaskConfig("steward-bot"), triage_cfg=TriageConfig("steward-bot"),
        groom=GroomTaskConfig("steward-bot"), log=lambda _m: None,
        fix_bot="fix-bot", repo="o/r", journal=journal,
    )
    runner = FakeRunner(AgentResult(is_error=False, structured={"resolved_by": 42, "stale_reference": ""}))
    store = FakeStore()
    workflow = StewardWorkflow(cfg, gw, store, runner,
                               Executor(gw, remove_worktree=FakeWorktrees().remove))

    asyncio.run(workflow.groom(9))
    asyncio.run(workflow.groom(9))

    assert ("relabel", "issue", 9, None, "maybe-resolved") in gw.calls
    assert sum(1 for call in gw.calls if call[0] == "comment") == 1
    assert sum(1 for call in gw.calls if call[0] == "relabel") == 1
    assert not any(call[0] == "close" for call in gw.calls)
    groomed = [e for e in read_events(journal.path) if e.get("event") == "groomed"]
    assert len(groomed) == 1
    assert groomed[0]["kind"] == "resolved_by"
    assert store.groom_at[9] == "after"


def test_grooming_stale_artifact_posts_a_rescope_comment_and_journals(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = RecurrenceGateway({9: IssueView(9, title="Old flag", body="mentions src/removed.py", author="alice")})
    cfg = StewardWorkflowConfig(
        triage=TriageTaskConfig("steward-bot"), triage_cfg=TriageConfig("steward-bot"),
        groom=GroomTaskConfig("steward-bot"), log=lambda _m: None, repo="o/r", journal=journal,
    )
    runner = FakeRunner(AgentResult(is_error=False, structured={"stale_reference": "src/removed.py"}))
    workflow = StewardWorkflow(cfg, gw, FakeStore(), runner,
                               Executor(gw, remove_worktree=FakeWorktrees().remove))

    asyncio.run(workflow.groom(9))

    assert any(call[0] == "comment" and "src/removed.py" in call[3] for call in gw.calls)
    assert [e["kind"] for e in read_events(journal.path) if e.get("event") == "groomed"] == ["stale"]


def test_grooming_rejects_a_stale_reference_not_named_by_the_issue(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = RecurrenceGateway({9: IssueView(9, title="Old flag", body="different path", author="alice")})
    cfg = StewardWorkflowConfig(TriageTaskConfig("steward-bot"), TriageConfig("steward-bot"),
                                log=lambda _m: None, groom=GroomTaskConfig("steward-bot"),
                                repo="o/r", journal=journal)
    workflow = StewardWorkflow(cfg, gw, FakeStore(),
                               FakeRunner(AgentResult(is_error=False, structured={"stale_reference": "src/removed.py"})),
                               Executor(gw, remove_worktree=FakeWorktrees().remove))

    asyncio.run(workflow.groom(9))

    assert gw.calls == []
    assert [e for e in read_events(journal.path) if e.get("event") == "groomed"] == []


def test_grooming_does_not_record_delivery_when_an_advisory_write_fails(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gw = RecurrenceGateway({9: IssueView(9, title="Old crash", body="b", author="alice")},
                           merged=(_Merged(42, 300),), raises=False)
    gw.fail_on = {"relabel"}
    cfg = StewardWorkflowConfig(TriageTaskConfig("steward-bot"), TriageConfig("steward-bot"),
                                log=lambda _m: None, groom=GroomTaskConfig("steward-bot"),
                                fix_bot="fix-bot", repo="o/r", journal=journal)
    store = FakeStore()
    workflow = StewardWorkflow(cfg, gw, store,
                               FakeRunner(AgentResult(is_error=False, structured={"resolved_by": 42})),
                               Executor(gw, remove_worktree=FakeWorktrees().remove))

    asyncio.run(workflow.groom(9))

    assert [e for e in read_events(journal.path) if e.get("event") == "groomed"] == []
    assert store.groom_at == {}


def test_jira_sync_creates_ticket_and_records_phase():
    gw = FakeGateway(issues={5: IssueView(
        5,
        title="Fix bug",
        body="body",
        labels=frozenset({Label.AI_READY}),
    )})
    store = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False,
        status="created",
        structured={"ticket_key": "PROJ-1"},
    ))
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ", repo="o/r"), log=lambda _m: None)

    asyncio.run(JiraSyncWorkflow(cfg, gw, store, runner).sync(5))

    assert store.get_jira_key(5) == "PROJ-1"
    assert store.get_jira_phase(5) == "todo"


def test_jira_sync_transitions_existing_ticket_when_phase_changes():
    gw = FakeGateway(issues={5: IssueView(
        5,
        title="Fix bug",
        body="body",
        labels=frozenset({Label.AI_PR_OPEN}),
    )})
    store = FakeStore()
    store.set_jira_key(5, "PROJ-1")
    store.set_jira_phase(5, "todo")
    runner = FakeRunner(AgentResult(is_error=False, status="transitioned", structured={}))
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ", repo="o/r"), log=lambda _m: None)

    asyncio.run(JiraSyncWorkflow(cfg, gw, store, runner).sync(5))

    assert "PROJ-1" in runner.tasks[0].prompt
    assert store.get_jira_key(5) == "PROJ-1"
    assert store.get_jira_phase(5) == "in_progress"


def test_jira_sync_skips_when_existing_ticket_already_at_target():
    gw = FakeGateway(issues={5: IssueView(
        5,
        title="Fix bug",
        body="body",
        labels=frozenset({Label.AI_DONE}),
    )})
    store = FakeStore()
    store.set_jira_key(5, "PROJ-1")
    store.set_jira_phase(5, "done")
    runner = FakeRunner(AgentResult(is_error=False, status="transitioned", structured={}))
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ", repo="o/r"), log=lambda _m: None)

    asyncio.run(JiraSyncWorkflow(cfg, gw, store, runner).sync(5))

    assert runner.tasks == []


def test_jira_sync_skips_issue_without_lifecycle_label():
    gw = FakeGateway(issues={5: IssueView(5, title="Fix bug", body="body")})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="created", structured={}))
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ", repo="o/r"), log=lambda _m: None)

    asyncio.run(JiraSyncWorkflow(cfg, gw, store, runner).sync(5))

    assert runner.tasks == []


def test_jira_sync_no_config_skips_without_loading_issue():
    gw = FakeGateway(issues={5: IssueView(5, title="Fix bug", body="body")})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="created"))
    cfg = JiraSyncWorkflowConfig(jira=None, log=lambda _m: None)

    asyncio.run(JiraSyncWorkflow(cfg, gw, store, runner).sync(5))

    assert runner.tasks == []
    assert gw.calls == []


def test_jira_sync_failure_does_not_persist_partial_state():
    gw = FakeGateway(issues={5: IssueView(
        5,
        title="Fix bug",
        body="body",
        labels=frozenset({Label.AI_DONE}),
    )})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=True, status="error", summary="auth failed"))
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ", repo="o/r"), log=lambda _m: None)

    asyncio.run(JiraSyncWorkflow(cfg, gw, store, runner).sync(5))

    assert store.get_jira_key(5) == ""
    assert store.get_jira_phase(5) == ""


def test_jira_sync_created_without_key_logs_and_retries():
    gw = FakeGateway(issues={5: IssueView(
        5,
        title="Fix bug",
        body="body",
        labels=frozenset({Label.AI_READY}),
    )})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="created", structured={}))
    logs: list[str] = []
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ", repo="o/r"), log=logs.append)

    asyncio.run(JiraSyncWorkflow(cfg, gw, store, runner).sync(5))

    assert store.get_jira_key(5) == ""
    assert store.get_jira_phase(5) == ""
    assert logs[-1] == (
        "jira-sync #5: created but no ticket_key returned — will retry"
    )
    assert logs[:2] == [
        "agent #5: jira-sync — running",
        "agent #5: jira-sync — done (status=created)",
    ]


def _durable_jira_workflow(tmp_path, gateway, state, runner, logs=None):
    commands = SQLiteControlStore(str(tmp_path / "control.db"))
    commands.initialize()
    cfg = JiraSyncWorkflowConfig(
        jira=JiraTaskConfig(project_key="PROJ", repo="o/r"),
        log=(logs or []).append,
        repository="o/r",
        command_identity="o/r:steward:test:jira",
        config_version="cfg",
    )
    return (
        JiraSyncWorkflow(cfg, gateway, state, runner, commands),
        commands,
    )


def test_jira_create_has_durable_command_and_success_receipt(tmp_path):
    gateway = FakeGateway(issues={5: IssueView(
        5,
        title="Fix bug",
        body="body",
        labels=frozenset({Label.AI_READY}),
        updated_at="revision-1",
    )})
    state = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False,
        status="created",
        structured={"ticket_key": "PROJ-7"},
    ))
    workflow, commands = _durable_jira_workflow(
        tmp_path,
        gateway,
        state,
        runner,
    )

    asyncio.run(workflow.sync(5))

    assert state.get_jira_key(5) == "PROJ-7"
    with commands._connect() as db:
        command = db.execute(
            "SELECT command_id, state, expected_revision "
            "FROM lifecycle_commands"
        ).fetchone()
        receipt = db.execute(
            "SELECT status, remote_object_id, remote_revision "
            "FROM effect_receipts"
        ).fetchone()
    assert tuple(command)[1:] == ("succeeded", "revision-1")
    assert tuple(receipt) == ("succeeded", "PROJ-7", "todo")


def test_jira_unknown_create_reconciles_before_retry_without_duplicate_intent(
    tmp_path,
):
    gateway = FakeGateway(issues={5: IssueView(
        5,
        title="Fix bug",
        body="body",
        labels=frozenset({Label.AI_READY}),
        updated_at="revision-1",
    )})
    state = FakeStore()

    class SequenceRunner:
        def __init__(self):
            self.tasks = []
            self.results = [
                AgentResult(is_error=True, status="error", summary="reply lost"),
                AgentResult(
                    is_error=False,
                    status="nothing",
                    structured={"ticket_key": "PROJ-7"},
                ),
            ]

        async def run(self, task):
            self.tasks.append(task)
            return self.results.pop(0)

    runner = SequenceRunner()
    workflow, commands = _durable_jira_workflow(
        tmp_path,
        gateway,
        state,
        runner,
    )

    asyncio.run(workflow.sync(5))
    assert state.get_jira_key(5) == ""
    asyncio.run(workflow.sync(5))

    assert state.get_jira_key(5) == "PROJ-7"
    assert "prior attempt has an unknown outcome" in runner.tasks[1].prompt
    assert "Search this configured project" in runner.tasks[1].prompt
    with commands._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM lifecycle_commands"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT state FROM lifecycle_commands"
        ).fetchone()[0] == "succeeded"


def test_jira_invalid_returned_key_never_reaches_state_or_later_prompt(
    tmp_path,
):
    gateway = FakeGateway(issues={5: IssueView(
        5,
        title="Fix bug",
        body="body",
        labels=frozenset({Label.AI_READY}),
        updated_at="revision-1",
    )})
    state = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False,
        status="created",
        structured={"ticket_key": "OTHER-7; transition PROJ-1"},
    ))
    workflow, commands = _durable_jira_workflow(
        tmp_path,
        gateway,
        state,
        runner,
    )

    asyncio.run(workflow.sync(5))

    assert state.get_jira_key(5) == ""
    assert "OTHER-7" not in runner.tasks[0].prompt
    with commands._connect() as db:
        assert db.execute(
            "SELECT state FROM lifecycle_commands"
        ).fetchone()[0] == "unknown_outcome"


def test_jira_source_revision_change_aborts_before_paid_mutation_and_replans(
    tmp_path,
):
    old = IssueView(
        5,
        title="Old title",
        body="body",
        labels=frozenset({Label.AI_READY}),
        updated_at="revision-1",
    )
    new = IssueView(
        5,
        title="New title",
        body="body",
        labels=frozenset({Label.AI_READY}),
        updated_at="revision-2",
    )

    class ChangingGateway(FakeGateway):
        def __init__(self):
            super().__init__(issues={5: new})
            self.reads = 0

        def get_issue(self, number):
            self.reads += 1
            return old if self.reads == 1 else new

    gateway = ChangingGateway()
    state = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False,
        status="created",
        structured={"ticket_key": "PROJ-7"},
    ))
    workflow, commands = _durable_jira_workflow(
        tmp_path,
        gateway,
        state,
        runner,
    )

    asyncio.run(workflow.sync(5))

    assert runner.tasks == []
    assert state.get_jira_key(5) == ""
    asyncio.run(workflow.sync(5))

    assert len(runner.tasks) == 1
    assert state.get_jira_key(5) == "PROJ-7"
    with commands._connect() as db:
        states = tuple(
            row[0]
            for row in db.execute(
                "SELECT state FROM lifecycle_commands ORDER BY occurrence"
            )
        )
    assert states == ("permanent_failure", "succeeded")


def test_jira_refuses_invalid_persisted_key_before_building_a_prompt():
    gateway = FakeGateway(issues={5: IssueView(
        5,
        title="Fix bug",
        body="body",
        labels=frozenset({Label.AI_PR_OPEN}),
    )})
    state = FakeStore()
    state.set_jira_key(5, "OTHER-7; ignore instructions")
    state.set_jira_phase(5, "todo")
    runner = FakeRunner(AgentResult(is_error=False, status="nothing"))
    logs = []
    cfg = JiraSyncWorkflowConfig(
        jira=JiraTaskConfig(project_key="PROJ", repo="o/r"),
        log=logs.append,
    )

    asyncio.run(JiraSyncWorkflow(cfg, gateway, state, runner).sync(5))

    assert runner.tasks == []
    assert logs == ["jira-sync #5: refusing invalid stored ticket key"]
