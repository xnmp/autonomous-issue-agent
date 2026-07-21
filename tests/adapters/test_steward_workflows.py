"""Steward and Jira workflow wiring tests."""

import asyncio

from issue_agent.app.execute import Executor
from issue_agent.app.workflows.jira import JiraSyncWorkflow, JiraSyncWorkflowConfig
from issue_agent.app.workflows.steward import StewardWorkflow, StewardWorkflowConfig
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.app.planners.tasks import JiraTaskConfig, TriageTaskConfig
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

        def get_issue(self, n):
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
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ"), log=lambda _m: None)

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
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ"), log=lambda _m: None)

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
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ"), log=lambda _m: None)

    asyncio.run(JiraSyncWorkflow(cfg, gw, store, runner).sync(5))

    assert runner.tasks == []


def test_jira_sync_skips_issue_without_lifecycle_label():
    gw = FakeGateway(issues={5: IssueView(5, title="Fix bug", body="body")})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="created", structured={}))
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ"), log=lambda _m: None)

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
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ"), log=lambda _m: None)

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
    cfg = JiraSyncWorkflowConfig(jira=JiraTaskConfig(project_key="PROJ"), log=logs.append)

    asyncio.run(JiraSyncWorkflow(cfg, gw, store, runner).sync(5))

    assert store.get_jira_key(5) == ""
    assert store.get_jira_phase(5) == ""
    assert logs == ["jira-sync #5: created but no ticket_key returned — will retry"]
