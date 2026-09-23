"""Completed rearm tasks consume their prepared workspace without recreating it."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.contracts import CommandId
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.resolve_split import ContractResetResult
from issue_agent.domain.resolve_workspace import (
    EpisodeWorkspaceHandoff,
    EpisodeWorkspacePrepared,
    NoPriorResolveAdmission,
    ObservedResolveBase,
)
from issue_agent.domain.workflow import CompletedRearm, ResolveEpisode, WorkflowSnapshot
from issue_agent.domain.workflow_identity import WorkflowBinding, WorkflowRevision
from issue_agent.domain.world import IssueView, PRView
from tests.fakes.workflow_authority import resolve_snapshot

from test_dispatch import FakeGateway, FakeRunner, FakeStore
from test_fix_workflow import _workflow


NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)
BRANCH = "fix/5-rearmed"


class RecordingWorktrees:
    def __init__(self, selected_branch: str, prepared_path: str):
        self.selected_branch = selected_branch
        self.prepared_path = prepared_path
        self.ensure_calls: list[tuple[int, str]] = []
        self.path_calls: list[EpisodeWorkspacePrepared] = []

    def ensure(self, issue: int, branch: str) -> str:
        self.ensure_calls.append((issue, branch))
        return f"/wt/issue-{issue}"

    def episode_workspace_path(self, completed: EpisodeWorkspacePrepared) -> str:
        self.path_calls.append(completed)
        return self.prepared_path

    def branch_for_issue(
        self, issue: int, fallback: str, *, preferred: str = "",
    ) -> str:
        del issue, fallback, preferred
        return self.selected_branch

    def remove(self, issue: int) -> None:
        del issue

    def prune_validate(self) -> int:
        return 0


def _completed_rearm() -> WorkflowSnapshot:
    source = resolve_snapshot(5)
    destination = WorkflowBinding(
        replace(source.revision.fence, generation=source.revision.fence.generation + 1),
        source.authority,
        "episode-5-rearmed",
    )
    base = ObservedResolveBase("o/r", "refs/heads/dev", "a" * 40, NOW)
    handoff = EpisodeWorkspaceHandoff(
        "request:rearm-5",
        source.binding,
        destination,
        NoPriorResolveAdmission(source.binding),
        BRANCH,
        base,
    )
    workspace = EpisodeWorkspacePrepared(
        handoff,
        destination,
        ContractResetResult(handoff.revision, handoff.archive_ref),
        NOW,
    )
    intent = CompletedRearm(
        "request:rearm-5",
        "gate:resolve-5",
        destination.fence.generation,
        (CommandId("command:rearm-5"),),
        workspace,
    )
    return WorkflowSnapshot(
        WorkflowRevision(destination.fence, 1),
        destination.authority,
        ResolveEpisode(destination.episode_id),
        intent=intent,
    )


def _completed_workflow(monkeypatch, tmp_path: Path, *, selected_branch: str = BRANCH):
    missing = tmp_path / "missing-prepared-worktree"
    worktrees = RecordingWorktrees(selected_branch, str(missing))
    gateway = FakeGateway(
        issues={5: IssueView(5, title="Rearmed issue", body="body")},
        pr_for_issue={},
    )
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="done"))
    workflow = _workflow(gateway, store, runner, worktrees)
    state = _completed_rearm()
    monkeypatch.setattr(
        workflow, "_resolve_state", lambda issue, binding=None: state,
    )
    return workflow, store, runner, worktrees, state, missing


def test_completed_rearm_missing_checkout_is_not_recreated_or_dispatched(
    monkeypatch, tmp_path: Path,
) -> None:
    workflow, store, runner, worktrees, state, missing = _completed_workflow(
        monkeypatch, tmp_path,
    )
    prepared: list[str] = []

    def require_existing_checkout(cwd: str) -> None:
        prepared.append(cwd)
        if not Path(cwd).is_dir():
            raise FileNotFoundError(cwd)

    workflow.prepare_cwd = require_existing_checkout

    with pytest.raises(FileNotFoundError, match="missing-prepared-worktree"):
        asyncio.run(workflow.resolve(5, binding=state.binding))

    assert worktrees.path_calls == [state.intent.workspace]
    assert worktrees.ensure_calls == []
    assert prepared == [str(missing)]
    assert runner.tasks == []
    with store._workflow_db._connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0


def test_completed_rearm_rejects_a_selected_branch_outside_the_handoff(
    monkeypatch, tmp_path: Path,
) -> None:
    workflow, store, runner, worktrees, state, _missing = _completed_workflow(
        monkeypatch, tmp_path, selected_branch="fix/5-wrong",
    )

    with pytest.raises(WorkflowAuthorityError, match="prepared workspace handoff"):
        asyncio.run(workflow.resolve(5, binding=state.binding))

    assert worktrees.path_calls == []
    assert worktrees.ensure_calls == []
    assert runner.tasks == []
    assert store.get_branch(5) == ""


def test_ordinary_resolve_still_ensures_its_worktree() -> None:
    worktrees = RecordingWorktrees(BRANCH, "/unused/prepared/path")
    gateway = FakeGateway(
        issues={5: IssueView(5, title="Ordinary issue", body="body")},
        prs={100: PRView(100)},
        pr_for_issue={5: 100},
    )
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="done"))

    asyncio.run(_workflow(gateway, store, runner, worktrees).resolve(5))

    assert worktrees.ensure_calls == [(5, BRANCH)]
    assert worktrees.path_calls == []
    assert runner.tasks[0].cwd == "/wt/issue-5"
