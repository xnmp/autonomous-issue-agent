"""Re-arm application recovery against real SQLite and isolated Git workspaces."""

from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.agent_publish import AgentPublicationBroker
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.app.workflows.resolve_rearm import ResolveRearmWorkflow
from issue_agent.app.execute import Executor
from issue_agent.app.roles import EffectRecovery
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.labels import Label
from issue_agent.domain.workflow import (
    CancelRearm, CancelledRearm, CompletedRearm, ContinueGrantedRearm, KeepGuidance,
    OperatorActor, PendingRearm, RequestRearm, ResolveEpisode, SetGuidance,
    SupersededRearm, WorkflowCommitted, WorkflowConflict, WorkflowRejected,
    WorkflowSnapshot,
)
from issue_agent.domain.workflow_policy import reconcile_resolve_ceiling
from issue_agent.domain.world import IssueView
from tests.adapters.test_episode_workspace import EpisodeRepo, episode_repo, _git
from tests.adapters.test_workflow_control import ACTOR, NOW, WRITER
from tests.fakes.in_memory_github import InMemoryGitHub
from tests.fakes.persistent_github import PersistentGitHub
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot


@pytest.fixture
def environment(tmp_path, episode_repo):
    clock = [NOW]
    store = SQLiteControlStore(str(tmp_path / "control.sqlite"), now=lambda: clock[0])
    store.initialize()
    state = replace(resolve_snapshot(), episode=ResolveEpisode("exhausted", 3))
    proposal = reconcile_resolve_ceiling(state, ceiling=3, evidence_ids=("prior-attempts",))
    state = proposal.snapshot
    install_workflow(store, state)
    gateway = InMemoryGitHub(issues=(IssueView(
        1, title="Rearm safely", body="Original requirements", updated_at="issue-v1",
        labels=frozenset({Label.NEEDS_HUMAN_REVIEW}),
    ),))
    logs = []

    def service():
        # Reconstruct adapters rather than retaining process-local grant state.
        control = SQLiteWorkflowControl(
            SQLiteControlStore(store.path, now=lambda: clock[0]), WRITER, resolve_ceiling=3,
        )
        return ResolveRearmWorkflow(
            control, gateway, episode_repo.worktrees, lambda: clock[0], logs.append,
        )

    command = RequestRearm("operator-request", state.revision, state.gates[0].gate_id, KeepGuidance())
    return service, store, gateway, episode_repo, clock, logs, command


def _state(service, command):
    current = service.control.load_workflow(command.expected.fence.key)
    assert isinstance(current, WorkflowSnapshot)
    return current


def _remote_projection(store, gateway, state, now):
    assert isinstance(state.intent, PendingRearm)
    command_id = state.intent.required_commands[0]
    store.begin_command(command_id)
    gateway.issues[1] = replace(gateway.issues[1], labels=frozenset({Label.AI_READY}), updated_at="issue-v2")
    store.record_effect(EffectReceipt(command_id, WRITER.owner_id, now, now, EffectStatus.SUCCEEDED))


def test_acceptance_is_durable_without_git_reset_paid_work_or_remote_writes(environment):
    service, store, gateway, repo, clock, logs, command = environment
    accepted = service().submit(command, ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    assert isinstance(accepted.snapshot.intent, PendingRearm)
    assert accepted.snapshot.episode.failures == 0
    assert accepted.snapshot.gates
    assert gateway.calls == []
    assert not any(args[:2] == ("reset", "--hard") for args, _ in repo.runner.calls)
    with closing(store._connect()) as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM lifecycle_commands WHERE state='pending'").fetchone()[0] == 1


def test_reconstructed_duplicate_skips_all_observation_and_changed_payload_conflicts(environment, monkeypatch):
    service, store, gateway, repo, clock, logs, command = environment
    accepted = service().submit(command, ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    gateway.fail_on.update({"get_issue", "pr_for_issue"})
    monkeypatch.setattr(repo.worktrees, "observe_and_pin_episode_base", lambda *a, **kw: pytest.fail("duplicate fetched Git"))
    clock[0] += timedelta(hours=1)
    replay = service().submit(command, OperatorActor(ACTOR.subject, "cli"))
    assert isinstance(replay, WorkflowCommitted) and replay.idempotent
    assert replay.snapshot == accepted.snapshot
    assert isinstance(service().submit(replace(command, guidance=SetGuidance("Different")), ACTOR), WorkflowConflict)
    assert isinstance(service().submit(command, OperatorActor("operator:bob", "cli")), WorkflowConflict)


def test_restart_grant_then_ack_then_remote_receipt_completes_without_second_reset(environment, monkeypatch):
    service, store, gateway, repo, clock, logs, command = environment
    assert isinstance(service().submit(command, ACTOR), WorkflowCommitted)
    clock[0] += timedelta(minutes=1)
    assert service().reconcile() == 1
    pending = _state(service(), command)
    assert isinstance(pending.intent, PendingRearm)
    prepared = service().control.prepared_workspace(pending.revision)
    assert prepared is not None
    cwd = Path(repo.worktrees.ensure(1, pending.intent.handoff.branch))
    assert _git(cwd, "rev-parse", "HEAD") == prepared.handoff.base.commit_sha
    monkeypatch.setattr(repo.worktrees, "prepare_episode_workspace", lambda *a, **kw: pytest.fail("acknowledged tree was reset twice"))
    _remote_projection(store, gateway, pending, clock[0])
    clock[0] += timedelta(minutes=1)
    assert service().reconcile() == 1
    final = _state(service(), command)
    assert isinstance(final.intent, CompletedRearm)
    assert final.intent.workspace == prepared
    assert final.gates == ()
    assert service().reconcile() == 0
    assert logs == []


def test_changed_requirements_supersede_before_any_workspace_reset(environment, monkeypatch):
    service, store, gateway, repo, clock, logs, command = environment
    assert isinstance(service().submit(command, ACTOR), WorkflowCommitted)
    gateway.issues[1] = replace(gateway.issues[1], body="New requirements", updated_at="changed")
    monkeypatch.setattr(repo.worktrees, "prepare_episode_workspace", lambda *a, **kw: pytest.fail("changed decision reset work"))
    clock[0] += timedelta(minutes=1)
    assert service().reconcile() == 1
    assert isinstance(_state(service(), command).intent, SupersededRearm)


def test_cancel_continue_reuses_original_preparation_without_another_grant(environment, monkeypatch):
    service, store, gateway, repo, clock, logs, command = environment
    assert isinstance(service().submit(command, ACTOR), WorkflowCommitted)
    clock[0] += timedelta(minutes=1)
    service().reconcile()
    pending = _state(service(), command)
    assert isinstance(pending.intent, PendingRearm)
    original = pending.intent.handoff
    cancelled = service().submit(CancelRearm("cancel-request", pending.revision, command.request_id, "pause"), ACTOR)
    assert isinstance(cancelled, WorkflowCommitted)
    assert isinstance(cancelled.snapshot.intent, CancelledRearm)
    continuation = ContinueGrantedRearm("continue-request", cancelled.snapshot.revision, command.request_id)
    monkeypatch.setattr(repo.worktrees, "observe_and_pin_episode_base", lambda *a, **kw: pytest.fail("continuation acquired a different base"))
    monkeypatch.setattr(repo.worktrees, "prepare_episode_workspace", lambda *a, **kw: pytest.fail("continuation repeated completed preparation"))
    continued = service().submit(continuation, ACTOR)
    assert isinstance(continued, WorkflowCommitted)
    assert continued.snapshot.episode == pending.episode
    assert isinstance(continued.snapshot.intent, PendingRearm)
    assert continued.snapshot.intent.handoff == original
    clock[0] += timedelta(minutes=1)
    service().reconcile()
    _remote_projection(store, gateway, _state(service(), command), clock[0])
    clock[0] += timedelta(minutes=1)
    service().reconcile()
    assert isinstance(_state(service(), command).intent, CompletedRearm)
    assert logs == []


def test_existing_ack_with_changed_tree_cannot_open_gate(environment):
    service, store, gateway, repo, clock, logs, command = environment
    service().submit(command, ACTOR)
    clock[0] += timedelta(minutes=1)
    service().reconcile()
    pending = _state(service(), command)
    assert isinstance(pending.intent, PendingRearm)
    _remote_projection(store, gateway, pending, clock[0])
    cwd = Path(repo.worktrees.ensure(1, pending.intent.handoff.branch))
    (cwd / "unreviewed.txt").write_text("changed after acknowledgement")
    clock[0] += timedelta(minutes=1)
    assert service().reconcile() == 0
    current = _state(service(), command)
    assert isinstance(current.intent, PendingRearm)
    assert current.gates
    assert (cwd / "unreviewed.txt").read_text() == "changed after acknowledgement"
    assert any("recovery_unavailable" in log for log in logs)
    previous_logs = tuple(logs)
    assert service().reconcile() == 0
    assert tuple(logs) == previous_logs


def test_unavailable_linkage_never_accepts_as_absence(environment):
    service, store, gateway, repo, clock, logs, command = environment
    gateway.fail_on.add("pr_for_issue")
    assert isinstance(service().submit(command, ACTOR), WorkflowRejected)
    assert _state(service(), command).revision == command.expected
    assert repo.runner.calls == []


def test_storage_failure_propagates_from_recovery(environment, monkeypatch):
    service, store, gateway, repo, clock, logs, command = environment
    service().submit(command, ACTOR)
    clock[0] += timedelta(minutes=1)
    workflow = service()
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("disk unavailable")
    monkeypatch.setattr(workflow.control, "reconcile_rearm", unavailable)
    with pytest.raises(sqlite3.OperationalError, match="disk unavailable"):
        workflow.reconcile()


def test_corrupt_peer_does_not_starve_healthy_workspace_recovery(environment):
    service, store, gateway, repo, clock, logs, command = environment
    service().submit(command, ACTOR)
    second = replace(resolve_snapshot(2), episode=ResolveEpisode("exhausted-two", 3))
    second = reconcile_resolve_ceiling(second, ceiling=3, evidence_ids=("prior-two",)).snapshot
    install_workflow(store, second)
    gateway.issues[2] = replace(gateway.issues[1], number=2)
    other = RequestRearm("healthy-request", second.revision, second.gates[0].gate_id, KeepGuidance())
    assert isinstance(service().submit(other, ACTOR), WorkflowCommitted)
    with store.transaction() as db:
        db.execute("UPDATE workflow_rearms SET actor_json='{}' WHERE request_id=?", (command.request_id,))
    clock[0] += timedelta(minutes=1)
    assert service().reconcile() == 1
    healthy = _state(service(), other)
    assert service().control.prepared_workspace(healthy.revision) is not None
    assert any("unavailable" in log and "operator-request" in log for log in logs)


def test_crash_after_git_before_ack_recovers_idempotent_preparation(environment, monkeypatch):
    service, store, gateway, repo, clock, logs, command = environment
    service().submit(command, ACTOR)
    clock[0] += timedelta(minutes=1)
    workflow = service()
    original = workflow.control.complete_workspace_handoff
    def crashed(*args, **kwargs):
        raise SystemExit("process died after Git")
    monkeypatch.setattr(workflow.control, "complete_workspace_handoff", crashed)
    with pytest.raises(SystemExit, match="process died"):
        workflow.reconcile()
    monkeypatch.setattr(workflow.control, "complete_workspace_handoff", original)
    pending = _state(service(), command)
    assert service().control.prepared_workspace(pending.revision) is None
    clock[0] += timedelta(minutes=1)
    assert service().reconcile() == 1
    recovered = _state(service(), command)
    assert service().control.prepared_workspace(recovered.revision) is not None


def test_changed_facts_during_preparation_supersede_before_effects(environment, monkeypatch):
    service, store, gateway, repo, clock, logs, command = environment
    service().submit(command, ACTOR)
    clock[0] += timedelta(minutes=1)
    prepare = repo.worktrees.prepare_episode_workspace
    def change_after_prepare(*args, **kwargs):
        result = prepare(*args, **kwargs)
        gateway.issues[1] = replace(gateway.issues[1], state="closed", updated_at="closed-after-preparation")
        return result
    monkeypatch.setattr(repo.worktrees, "prepare_episode_workspace", change_after_prepare)
    assert service().reconcile() == 1
    assert isinstance(_state(service(), command).intent, SupersededRearm)
    assert gateway.calls == []


def test_duplicate_cancellation_returns_original_success_without_refetch(environment):
    service, store, gateway, repo, clock, logs, command = environment
    accepted = service().submit(command, ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    cancel = CancelRearm("cancellation", accepted.snapshot.revision, command.request_id, "pause")
    first = service().submit(cancel, ACTOR)
    assert isinstance(first, WorkflowCommitted)
    gateway.fail_on.add("get_issue")
    replay = service().submit(cancel, OperatorActor(ACTOR.subject, "cli"))
    assert isinstance(replay, WorkflowCommitted) and replay.idempotent
    assert replay.snapshot == first.snapshot
    assert isinstance(service().submit(replace(cancel, reason="different"), ACTOR), WorkflowConflict)


@pytest.mark.parametrize("invalid", [None, {}, "request", 1])
def test_invalid_transport_values_are_controlled_rejections(environment, invalid):
    service, store, gateway, repo, clock, logs, command = environment
    assert isinstance(service().submit(invalid, ACTOR), WorkflowRejected)
    assert isinstance(service().submit(command, invalid), WorkflowRejected)
    assert _state(service(), command).revision == command.expected
    assert repo.runner.calls == []


def _replay_client(service, store, remote, clock, logs):
    workflow = replace(service(), gateway=remote)
    effects = GitHubEffectExecutor(
        remote, store, repository="o/r", identity=WRITER.owner_id, log=logs.append,
    )
    effects.effects.now = lambda: clock[0]
    executor = Executor(
        remote, effect_executor=effects, retry_delays=(), log=logs.append,
        run_agent=lambda _: pytest.fail("re-arm recovery dispatched a paid leg"),
    )
    executor.bind_workflow_reconciliation(workflow.reconcile)
    recovery = EffectRecovery(effects, AgentPublicationBroker(
        store, repository="o/r", base_branch="dev", identity=f"{WRITER.owner_id}:publication",
        owner=WRITER.owner_id,
    ), reconcile_workflow=workflow.reconcile)
    return workflow, effects, executor, recovery


@pytest.mark.parametrize("entry", ["executor", "recovery"])
def test_real_effect_recovery_prepares_before_remote_write_without_world_or_paid_dispatch(environment, tmp_path, monkeypatch, entry):
    service, store, gateway, repo, clock, logs, command = environment
    remote = PersistentGitHub(tmp_path / "remote.sqlite")
    remote.seed(gateway.issues[1])
    workflow, effects, executor, recovery = _replay_client(service, store, remote, clock, logs)
    monkeypatch.setattr(remote, "build_world", lambda: pytest.fail("recovery required a world observation"))
    accepted = workflow.submit(command, ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    assert isinstance(accepted.snapshot.intent, PendingRearm)
    command_id = accepted.snapshot.intent.required_commands[0]
    # A naked drain cannot release the ready label before the Git obligation.
    assert effects.drain_pending_commands() == 0
    assert remote.calls == []
    assert workflow.control.prepared_workspace(accepted.snapshot.revision) is None
    clock[0] += timedelta(minutes=1)
    if entry == "executor":
        assert executor.replay_pending()
    else:
        assert recovery.drain_pending_commands() >= 1
    pending = _state(workflow, command)
    assert isinstance(pending.intent, PendingRearm)
    assert workflow.control.prepared_workspace(pending.revision) is not None
    assert remote.get_issue(1).labels == frozenset({Label.AI_READY})
    receipt = store.effect_receipt(command_id)
    assert receipt is not None and receipt.status is EffectStatus.SUCCEEDED
    # A reconstructed process uses the receipt and fresh workspace observation.
    workflow, effects, executor, recovery = _replay_client(service, store, PersistentGitHub(remote.path), clock, logs)
    clock[0] += timedelta(minutes=1)
    assert executor.replay_pending()
    assert isinstance(_state(workflow, command).intent, CompletedRearm)
    assert len(remote.calls) == 1
    with closing(store._connect()) as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert logs == []


def _die_after_rearm_remote_success(control_path, remote_path, now):
    store = SQLiteControlStore(control_path, now=lambda: now)
    remote = PersistentGitHub(Path(remote_path))
    effects = GitHubEffectExecutor(remote, store, repository="o/r", identity=WRITER.owner_id)
    effects.effects.now = lambda: now
    store.record_effect = lambda _: os._exit(71)
    effects.drain_pending_commands()


def test_real_process_death_after_remote_success_before_receipt_reconciles_without_duplicate(environment, tmp_path):
    service, store, gateway, repo, clock, logs, command = environment
    remote = PersistentGitHub(tmp_path / "remote.sqlite")
    remote.seed(gateway.issues[1])
    workflow, effects, executor, recovery = _replay_client(service, store, remote, clock, logs)
    accepted = workflow.submit(command, ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    assert isinstance(accepted.snapshot.intent, PendingRearm)
    command_id = accepted.snapshot.intent.required_commands[0]
    clock[0] += timedelta(minutes=1)
    assert workflow.reconcile() == 1
    process = multiprocessing.get_context("spawn").Process(
        target=_die_after_rearm_remote_success, args=(store.path, str(remote.path), clock[0]),
    )
    process.start()
    try:
        process.join(timeout=15)
        assert process.exitcode == 71
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert remote.get_issue(1).labels == frozenset({Label.AI_READY})
    assert store.effect_receipt(command_id) is None
    assert store.pending_commands()[0].state == "executing"
    restarted_store = SQLiteControlStore(store.path, now=lambda: clock[0])
    workflow, effects, executor, recovery = _replay_client(service, restarted_store, PersistentGitHub(remote.path), clock, logs)
    for _ in range(3):
        clock[0] += timedelta(minutes=1)
        assert executor.replay_pending()
    assert isinstance(_state(workflow, command).intent, CompletedRearm)
    receipt = restarted_store.effect_receipt(command_id)
    assert receipt is not None and receipt.status is EffectStatus.SUCCEEDED
    assert len(remote.calls) == 1
    with closing(store._connect()) as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert logs == []


def test_real_success_receipt_cannot_complete_dirty_acknowledged_workspace(environment, tmp_path):
    service, store, gateway, repo, clock, logs, command = environment
    remote = PersistentGitHub(tmp_path / "remote.sqlite")
    remote.seed(gateway.issues[1])
    workflow, effects, executor, recovery = _replay_client(service, store, remote, clock, logs)
    accepted = workflow.submit(command, ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    clock[0] += timedelta(minutes=1)
    assert executor.replay_pending()
    pending = _state(workflow, command)
    assert isinstance(pending.intent, PendingRearm)
    receipt = store.effect_receipt(pending.intent.required_commands[0])
    assert receipt is not None and receipt.status is EffectStatus.SUCCEEDED
    cwd = Path(repo.worktrees.ensure(1, pending.intent.handoff.branch))
    (cwd / "changed-after-remote.txt").write_text("preserve this unreviewed work")
    workflow, effects, executor, recovery = _replay_client(service, store, PersistentGitHub(remote.path), clock, logs)
    clock[0] += timedelta(minutes=1)
    assert executor.replay_pending()
    current = _state(workflow, command)
    assert isinstance(current.intent, PendingRearm) and current.gates
    assert (cwd / "changed-after-remote.txt").read_text() == "preserve this unreviewed work"
    assert len(remote.calls) == 1
    assert any("recovery_unavailable" in line for line in logs)


def _read_rearm_observation(store, request_id):
    with closing(store._connect()) as db:
        row = db.execute(
            "SELECT claimed_at,payload_json FROM dedupe_keys "
            "WHERE namespace='workflow-rearm-observation' AND scope='o/r' AND key=?",
            (request_id,),
        ).fetchone()
        assert row is not None
        return tuple(row)


def test_stale_selected_revision_does_not_charge_unobserved_current_authority(environment, monkeypatch):
    service, store, gateway, repo, clock, logs, command = environment
    accepted = service().submit(command, ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    clock[0] += timedelta(minutes=1)
    workflow = service()
    select = workflow.control.pending_rearms
    before = _read_rearm_observation(store, command.request_id)
    def changed_after_selection(*args, **kwargs):
        selected = select(*args, **kwargs)
        assert selected
        with store.transaction() as db:
            db.execute("UPDATE workflows SET version=version+1 WHERE repository='o/r' AND target_kind='issue' AND target_number=1")
        return selected
    monkeypatch.setattr(workflow.control, "pending_rearms", changed_after_selection)
    monkeypatch.setattr(workflow.control, "defer_rearm", lambda *a, **kw: pytest.fail("stale acquisition charged a new revision"))
    monkeypatch.setattr(gateway, "get_issue", lambda *a: pytest.fail("stale acquisition fetched unowned facts"))
    assert workflow.reconcile() == 0
    after = _state(workflow, command)
    assert after.revision.version == accepted.snapshot.revision.version + 1
    assert after.gates == accepted.snapshot.gates
    assert _read_rearm_observation(store, command.request_id) == before
    assert any("recovery_unavailable" in line for line in logs)


def test_failed_observation_cannot_defer_concurrently_replaced_revision(environment, monkeypatch):
    service, store, gateway, repo, clock, logs, command = environment
    accepted = service().submit(command, ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    clock[0] += timedelta(minutes=1)
    workflow = service()
    captured = []
    def concurrent_revision(*args, **kwargs):
        captured.append(_read_rearm_observation(store, command.request_id))
        # Models authority changing before the failure handler's CAS, not a
        # legitimate writer bypassing the deployment barrier in production.
        with store.transaction() as db:
            db.execute("UPDATE workflows SET version=version+1 WHERE repository='o/r' AND target_kind='issue' AND target_number=1")
        raise ValueError("preparation observation failed")
    monkeypatch.setattr(repo.worktrees, "prepare_episode_workspace", concurrent_revision)
    assert workflow.reconcile() == 0
    assert _read_rearm_observation(store, command.request_id) == captured[0]
    after = _state(workflow, command)
    assert after.gates == accepted.snapshot.gates
    assert any("defer_unavailable" in line for line in logs)


@pytest.mark.parametrize("initial_holds,fresh_holds", [
    ((), ("independent machine hold",)),
    (("independent machine hold",), ()),
])
def test_hold_drift_supersedes_before_git_then_continue_completes_fresh_projection(environment, tmp_path, monkeypatch, initial_holds, fresh_holds):
    service, store, gateway, repo, clock, logs, command = environment
    holds = [initial_holds]
    scoped_service = lambda: replace(service(), external_holds=lambda _: holds[0])
    remote = PersistentGitHub(tmp_path / "remote.sqlite")
    remote.seed(gateway.issues[1])
    workflow, effects, executor, recovery = _replay_client(scoped_service, store, remote, clock, logs)
    accepted = workflow.submit(command, ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    assert isinstance(accepted.snapshot.intent, PendingRearm)
    original_handoff = accepted.snapshot.intent.handoff
    holds[0] = fresh_holds
    prepare = repo.worktrees.prepare_episode_workspace
    monkeypatch.setattr(repo.worktrees, "prepare_episode_workspace", lambda *a, **kw: pytest.fail("obsolete hold projection prepared workspace"))
    clock[0] += timedelta(minutes=1)
    assert workflow.reconcile() == 1
    stopped = _state(workflow, command)
    assert isinstance(stopped.intent, SupersededRearm)
    assert stopped.episode == accepted.snapshot.episode
    assert remote.calls == []
    monkeypatch.setattr(repo.worktrees, "prepare_episode_workspace", prepare)
    continued = workflow.submit(ContinueGrantedRearm("continue-fresh-holds", stopped.revision, command.request_id), ACTOR)
    assert isinstance(continued, WorkflowCommitted)
    assert isinstance(continued.snapshot.intent, PendingRearm)
    assert continued.snapshot.intent.handoff == original_handoff
    assert continued.snapshot.episode == accepted.snapshot.episode
    required = continued.snapshot.intent.required_commands[0]
    clock[0] += timedelta(minutes=1)
    assert executor.replay_pending()
    receipt = store.effect_receipt(required)
    assert receipt is not None and receipt.status is EffectStatus.SUCCEEDED
    expected_label = Label.NEEDS_HUMAN_REVIEW if fresh_holds else Label.AI_READY
    assert remote.get_issue(1).labels == frozenset({expected_label})
    clock[0] += timedelta(minutes=1)
    assert executor.replay_pending()
    completed = _state(workflow, command)
    assert isinstance(completed.intent, CompletedRearm)
    assert completed.intent.handoff == original_handoff
    assert completed.episode == accepted.snapshot.episode
    assert logs == []
