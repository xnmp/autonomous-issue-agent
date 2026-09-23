"""Real-Git first paid entry is coupled to the durable workspace handoff."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
import threading
import sqlite3
import pytest

from issue_agent.adapters.control_store import AccountingDisposition, CorruptControlStore, RetiredOwnerError
from issue_agent.adapters.workspace_admission import WorkspaceAdmission
from issue_agent.adapters.deployment_authority import deployment_effect_barrier
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.worktree import Worktrees
from issue_agent.domain.resolve_workspace import EpisodeWorkspacePrepared
from issue_agent.domain.workflow import (
    KeepGuidance, NoLinkedPr, RequestRearm, ResolveAdmission, ResolveEpisode,
)
from issue_agent.domain.workflow_policy import (
    reconcile_resolve_ceiling, request_resolve_rearm,
)
from tests.adapters.test_episode_workspace import (
    BRANCH, EpisodeRepo, _git, episode_repo,
)
from tests.adapters.test_workflow_admission import request as paid_request
from tests.adapters.test_workflow_control import (
    ACTOR, NOW, WRITER, adapter, commit, control, enrich, observation,
    resolve_snapshot,
)
from tests.adapters.test_workspace_rearm_admission import complete as complete_rearm
from tests.fakes.workflow_authority import install_workflow


def _completed_episode(control, repository: EpisodeRepo):
    before = reconcile_resolve_ceiling(
        replace(resolve_snapshot(17), episode=ResolveEpisode("old-episode", 3)),
        ceiling=3,
        evidence_ids=("import:workspace-entry",),
    ).snapshot
    install_workflow(control, before)
    workflow = adapter(control)
    with workflow.rearm_authority(before.revision) as authorized:
        base = repository.worktrees.observe_and_pin_episode_base(
            "o/r", observed_at=NOW,
        )
        observed = workflow.observe_workspace(authorized.binding, base, BRANCH)
    gate = next(item for item in before.gates if getattr(item.cause, "episode_id", None) == before.episode.episode_id)
    proposal = enrich(request_resolve_rearm(
        before,
        RequestRearm("request:workspace-entry", before.revision, gate.gate_id, KeepGuidance()),
        ACTOR,
        observation(before),
        NoLinkedPr(before.revision.fence.key, NOW),
        now=NOW,
        active_attempts=(),
        workspace=observed,
    ))
    pending = commit(control, proposal).snapshot
    with workflow.workspace_handoff_authority(
        pending.intent.handoff.handoff_id, pending.revision,
    ) as existing:
        assert existing is None
        result = repository.worktrees.prepare_episode_workspace(pending.intent.handoff)
        acknowledgement = workflow.complete_workspace_handoff(
            EpisodeWorkspacePrepared(pending.intent.handoff, pending.binding, result, NOW)
        )
    completed = complete_rearm(control, pending, acknowledgement)
    return completed, acknowledgement


def _request(name, completed, cwd, *, stage="RESOLVE"):
    original = paid_request(name, binding=completed.binding, stage=stage)
    metadata = json.loads(original.dispatch_id)
    metadata.update({"cwd": cwd, "publication_branch": completed.intent.handoff.branch})
    return replace(
        original,
        dispatch_id=json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        owner_id=WRITER.owner_id,
    )


def _counts(control):
    with control._connect() as db:
        return (
            db.execute("SELECT count(*) FROM runs").fetchone()[0],
            db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0],
        )


def test_unbound_reservation_delegates_without_workspace_verification(control, episode_repo):
    request = paid_request("unbound", stage="TRIAGE")
    request = replace(request, workflow=None, owner_id=WRITER.owner_id)
    assert WorkspaceAdmission(control, episode_repo.worktrees)(request).admitted


def test_direct_reserve_cannot_bypass_first_workspace_verification(control, episode_repo):
    completed, acknowledgement = _completed_episode(control, episode_repo)
    cwd = episode_repo.worktrees.episode_workspace_path(acknowledgement)
    request = _request("direct", completed, cwd)
    refused = control.reserve(request)
    assert refused.disposition is AccountingDisposition.UNAVAILABLE
    assert "workspace verification" in refused.detail
    assert _counts(control) == (0, 0)


def test_tamper_before_first_entry_refuses_without_reserving(control, episode_repo):
    completed, acknowledgement = _completed_episode(control, episode_repo)
    cwd = episode_repo.worktrees.episode_workspace_path(acknowledgement)
    open(os.path.join(cwd, "untracked-secret.txt"), "w", encoding="utf-8").write("tampered\n")
    result = WorkspaceAdmission(control, episode_repo.worktrees)(
        _request("tampered", completed, cwd)
    )
    assert result.disposition is AccountingDisposition.UNAVAILABLE
    assert _counts(control) == (0, 0)


def test_first_route_consumes_proof_and_later_dirty_resolve_does_not_require_pristine(control, episode_repo):
    completed, acknowledgement = _completed_episode(control, episode_repo)
    cwd = episode_repo.worktrees.episode_workspace_path(acknowledgement)
    admission = WorkspaceAdmission(control, episode_repo.worktrees)
    assert admission(_request("route", completed, cwd, stage="ROUTE")).admitted
    open(os.path.join(cwd, "implementation.txt"), "w", encoding="utf-8").write("work\n")
    assert admission(_request("resolve", completed, cwd)).admitted
    assert _counts(control) == (2, 2)


def test_unlaunched_first_admission_still_consumes_workspace_proof(control, episode_repo):
    completed, acknowledgement = _completed_episode(control, episode_repo)
    cwd = episode_repo.worktrees.episode_workspace_path(acknowledgement)
    admission = WorkspaceAdmission(control, episode_repo.worktrees)
    assert admission(_request("unlaunched", completed, cwd, stage="ROUTE")).admitted
    assert control.release_unlaunched("unlaunched", "provider was never started").disposition is AccountingDisposition.AVAILABLE
    open(os.path.join(cwd, "retry-work.txt"), "w", encoding="utf-8").write("retained\n")
    assert admission(_request("after-release", completed, cwd)).admitted


def test_failed_reservation_rolls_back_entry_and_retry_reverifies(control, episode_repo):
    completed, acknowledgement = _completed_episode(control, episode_repo)
    cwd = episode_repo.worktrees.episode_workspace_path(acknowledgement)
    admission = WorkspaceAdmission(control, episode_repo.worktrees)
    request = _request("crash", completed, cwd)
    with control.transaction() as db:
        db.execute("CREATE TRIGGER reject_first_entry BEFORE INSERT ON runs "
                   "BEGIN SELECT RAISE(ABORT,'injected reservation failure'); END")
    assert admission(request).disposition is AccountingDisposition.UNAVAILABLE
    assert _counts(control) == (0, 0)
    with control.transaction() as db:
        db.execute("DROP TRIGGER reject_first_entry")
    open(os.path.join(cwd, "dirty-after-rollback.txt"), "w", encoding="utf-8").write("dirty\n")
    assert admission(request).disposition is AccountingDisposition.UNAVAILABLE
    assert _counts(control) == (0, 0)
    os.unlink(os.path.join(cwd, "dirty-after-rollback.txt"))
    assert admission(request).admitted


class CountingWorktrees(Worktrees):
    def __init__(self, repository: EpisodeRepo):
        super().__init__(str(repository.root), run=repository.runner)
        self.verifications = 0
        self._count_lock = threading.Lock()

    def verify_episode_workspace(self, completed, *, approved_host="github.com"):
        with self._count_lock:
            self.verifications += 1
        return super().verify_episode_workspace(completed, approved_host=approved_host)


def test_concurrent_first_entry_verifies_once_and_replays_one_admission(control, episode_repo):
    completed, acknowledgement = _completed_episode(control, episode_repo)
    worktrees = CountingWorktrees(episode_repo)
    cwd = worktrees.episode_workspace_path(acknowledgement)
    admission = WorkspaceAdmission(control, worktrees)
    request = _request("concurrent", completed, cwd, stage="ROUTE")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: admission(request), range(2)))
    assert all(result.admitted for result in results)
    assert worktrees.verifications == 1
    assert _counts(control) == (1, 1)


def test_dispatch_must_name_exact_verified_path_and_branch(control, episode_repo):
    completed, acknowledgement = _completed_episode(control, episode_repo)
    cwd = episode_repo.worktrees.episode_workspace_path(acknowledgement)
    admission = WorkspaceAdmission(control, episode_repo.worktrees)
    wrong_path = _request("wrong-path", completed, os.path.dirname(cwd))
    wrong_branch = _request("wrong-branch", completed, cwd)
    metadata = json.loads(wrong_branch.dispatch_id)
    metadata["publication_branch"] = "fix/17-foreign"
    wrong_branch = replace(wrong_branch, dispatch_id=json.dumps(metadata))
    assert admission(wrong_path).disposition is AccountingDisposition.UNAVAILABLE
    assert admission(wrong_branch).disposition is AccountingDisposition.UNAVAILABLE
    assert _counts(control) == (0, 0)


@pytest.mark.parametrize("completed_entry", [False, True])
def test_ordinary_admission_does_not_wait_for_unrelated_review_effect(control, episode_repo, completed_entry):
    admission = WorkspaceAdmission(control, episode_repo.worktrees)
    if completed_entry:
        state, acknowledgement = _completed_episode(control, episode_repo)
        cwd = episode_repo.worktrees.episode_workspace_path(acknowledgement)
        assert admission(_request("first-route", state, cwd, stage="ROUTE")).admitted
        request = _request("next-work", state, cwd)
    else:
        state = resolve_snapshot(17)
        install_workflow(control, state)
        request = paid_request("ordinary", binding=state.binding)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with deployment_effect_barrier(control.path, "o/r", "review"):
            future = pool.submit(admission, request)
            # Review effects have no authority over normal fix reservation.
            result = future.result(timeout=3)
            assert result.admitted


def test_corrupt_consumed_handoff_receipt_cannot_skip_first_entry_proof(control, episode_repo):
    completed, acknowledgement = _completed_episode(control, episode_repo)
    cwd = episode_repo.worktrees.episode_workspace_path(acknowledgement)
    admission = WorkspaceAdmission(control, episode_repo.worktrees)
    assert admission(_request("first", completed, cwd, stage="ROUTE")).admitted
    with control.transaction() as db:
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' AND key='first'").fetchone()
        receipt = decode_workflow(row[0], ResolveAdmission)
        assert receipt.workspace_handoff_id == acknowledgement.handoff.handoff_id
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='workflow-admission' AND key='first'",
                   (encode_workflow(replace(receipt, workspace_handoff_id=None)),))
    refused = admission(_request("second", completed, cwd))
    assert refused.disposition is AccountingDisposition.UNAVAILABLE
    assert _counts(control) == (1, 1)


@pytest.mark.parametrize("failure,disposition", [
    (CorruptControlStore("damaged authority"), AccountingDisposition.CORRUPT),
    (sqlite3.OperationalError("unreadable authority"), AccountingDisposition.UNAVAILABLE),
    (RetiredOwnerError("retired authority"), AccountingDisposition.UNAVAILABLE),
])
def test_entry_read_failure_preserves_accounting_disposition(control, episode_repo, monkeypatch, failure, disposition):
    def unavailable(_request):
        raise failure

    monkeypatch.setattr(control, "requires_workspace_entry", unavailable)
    request = paid_request("unavailable", binding=resolve_snapshot(17).binding)
    refused = WorkspaceAdmission(control, episode_repo.worktrees)(request)
    assert refused.disposition is disposition
    assert not refused.admitted
    assert _counts(control) == (0, 0)
