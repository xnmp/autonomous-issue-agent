"""Durable recovery and reconciliation of accepted resolve re-arms."""

from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.labels import Label
from issue_agent.domain.resolve_rearm import (
    PendingResolveRearm, RearmObservation, UnavailableResolveRearm,
)
from issue_agent.domain.workflow import (
    CancelRearm, CancelledRearm, CompletedRearm, NoLinkedPr, OperatorActor,
    PendingRearm, ResolveEpisode, StopReason, SupersededRearm, WorkflowCommitted,
    WorkflowConflict,
)
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey
from issue_agent.domain.workflow_policy import (
    cancel_resolve_rearm,
    reconcile_resolve_ceiling,
    supersede_resolve_rearm,
)
from tests.adapters.test_workflow_control import (
    ACTOR, NOW, WRITER, adapter, command_row, commit, control, evidence, held,
    observation, prepare_workspace, request, resolve_snapshot,
)
from tests.fakes.workflow_authority import install_workflow


def _advance(control, seconds=31):
    current = NOW + timedelta(seconds=seconds)
    control._now = lambda: current
    return current


def _pending(control, *, number=1, request_id="request:1"):
    before = held() if number == 1 else reconcile_resolve_ceiling(
        replace(resolve_snapshot(number), episode=ResolveEpisode(f"episode-{number}", 3)),
        ceiling=3, evidence_ids=(f"import:{number}",),
    ).snapshot
    install_workflow(control, before)
    proposal = request(before, request_id)
    pending = commit(control, proposal).snapshot
    assert isinstance(pending.intent, PendingRearm)
    return before, proposal, pending


def _succeed_required_projection(control, proposal, pending):
    prepare_workspace(control, pending)
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    receipt = EffectReceipt(
        command_id, WRITER.owner_id, NOW, NOW, EffectStatus.SUCCEEDED,
    )
    control.record_effect(receipt)
    return command_id, receipt


def test_accepted_rearm_returns_canonical_audit_and_refuses_foreign_or_corrupt_identity(control):
    before, proposal, _ = _pending(control)
    workflow = adapter(control)
    assert workflow.accepted_rearm(before.revision.fence.key, "request:1") == proposal.audit

    other = resolve_snapshot(2)
    install_workflow(control, other)
    with pytest.raises(WorkflowAuthorityError, match="another workflow"):
        workflow.accepted_rearm(other.revision.fence.key, "request:1")

    with control.transaction() as db:
        db.execute("UPDATE workflow_rearms SET actor_json='{}' WHERE request_id='request:1'")
    with pytest.raises(WorkflowAuthorityError, match="accepted re-arm audit"):
        workflow.accepted_rearm(before.revision.fence.key, "request:1")


def test_pending_inventory_due_time_deferral_and_restart_are_durable(control):
    _, _, pending = _pending(control)
    workflow = adapter(control)
    assert workflow.pending_rearms() == ()
    now = _advance(control)
    item = workflow.pending_rearms()[0]
    assert isinstance(item, PendingResolveRearm)
    assert item.snapshot == pending and item.observation.history.failed_reads == 0

    workflow.defer_rearm(pending.revision, "github read unavailable")
    assert workflow.pending_rearms() == ()
    restarted = adapter(SQLiteControlStore(control.path, now=lambda: now + timedelta(seconds=31)))
    deferred = restarted.pending_rearms()[0]
    assert isinstance(deferred, PendingResolveRearm)
    assert deferred.observation.last_error == "github read unavailable"
    assert deferred.observation.history.failed_reads == 1


def test_corrupt_pending_item_is_isolated_and_backed_off_for_an_hour(control):
    _, _, first = _pending(control, request_id="request:first")
    _, _, second = _pending(control, number=2, request_id="request:second")
    now = _advance(control)
    with control.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET payload_json='{}',claimed_at=? "
            "WHERE namespace='workflow-rearm-observation' AND key='request:first'",
            (now.isoformat(),),
        )
        db.execute(
            "UPDATE dedupe_keys SET claimed_at=? "
            "WHERE namespace='workflow-rearm-observation' AND key='request:second'",
            (now.isoformat(),),
        )
    items = adapter(control).pending_rearms()
    assert any(isinstance(item, UnavailableResolveRearm) and item.request_id == first.intent.request_id for item in items)
    assert any(isinstance(item, PendingResolveRearm) and item.snapshot == second for item in items)
    repeated = adapter(control).pending_rearms()
    assert len(repeated) == 1 and isinstance(repeated[0], PendingResolveRearm)
    assert repeated[0].snapshot == second


def test_remote_terminal_fact_supersedes_before_workspace_preparation(control):
    _, _, pending = _pending(control)
    now = _advance(control)
    closed = observation(pending, at=now)
    closed = replace(closed, issue=replace(closed.issue, state="closed"))
    result = adapter(control).reconcile_rearm(
        pending.revision, closed, NoLinkedPr(pending.revision.fence.key, now),
    )
    assert isinstance(result, WorkflowCommitted)
    assert isinstance(result.snapshot.intent, SupersededRearm)
    assert command_row(control, pending.intent.required_commands[0])["attempts"] == 0


def test_successful_remote_read_without_workspace_keeps_failed_history(control):
    _, _, pending = _pending(control)
    now = _advance(control)
    workflow = adapter(control)
    workflow.defer_rearm(pending.revision, "workspace verification unavailable")
    due = now + timedelta(seconds=31)
    control._now = lambda: due
    result = workflow.reconcile_rearm(
        pending.revision,
        observation(pending, at=due),
        NoLinkedPr(pending.revision.fence.key, due),
    )
    assert isinstance(result.snapshot.intent, PendingRearm)
    with closing(control._connect()) as db:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-rearm-observation'"
        ).fetchone()
    retained = decode_workflow(row[0], RearmObservation)
    assert retained.last_error == "workspace verification unavailable"
    assert retained.history.failed_reads == 1
    assert workflow.pending_rearms() == ()


def test_completion_requires_caller_verified_exact_ack_and_success_receipt(control):
    _, proposal, pending = _pending(control)
    now = _advance(control)
    workflow = adapter(control)
    acknowledgement = prepare_workspace(control, pending)
    assert workflow.prepared_workspace(pending.revision) == acknowledgement
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    control.record_effect(EffectReceipt(
        command_id, WRITER.owner_id, now, now, EffectStatus.SUCCEEDED,
    ))
    issue = observation(pending, frozenset({Label.AI_READY}), at=now)
    linkage = NoLinkedPr(pending.revision.fence.key, now)

    waiting = workflow.reconcile_rearm(pending.revision, issue, linkage)
    assert isinstance(waiting.snapshot.intent, PendingRearm)
    wrong = replace(acknowledgement, observed_at=acknowledgement.observed_at + timedelta(seconds=1))
    with pytest.raises(WorkflowAuthorityError, match="exact verified workspace"):
        workflow.reconcile_rearm(
            pending.revision, issue, linkage, workspace_verified=wrong,
        )
    completed = workflow.reconcile_rearm(
        pending.revision, issue, linkage, workspace_verified=acknowledgement,
    )
    assert isinstance(completed.snapshot.intent, CompletedRearm)


def test_permanent_projection_rejection_supersedes_without_workspace_proof(control):
    _, proposal, pending = _pending(control)
    now = _advance(control)
    prepare_workspace(control, pending)
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    control.record_effect(EffectReceipt(
        command_id, WRITER.owner_id, now, now,
        EffectStatus.PERMANENT_FAILURE, error="stale revision",
    ))
    result = adapter(control).reconcile_rearm(
        pending.revision,
        observation(pending, at=now),
        NoLinkedPr(pending.revision.fence.key, now),
    )
    assert isinstance(result.snapshot.intent, SupersededRearm)


def test_cancellation_replay_precedes_stale_cas_and_changed_identity_conflicts(control):
    _, _, pending = _pending(control)
    workflow = adapter(control)
    command = CancelRearm(
        "cancel:1", pending.revision, pending.intent.request_id, "operator cancelled",
    )
    cancelled = workflow.cancel_rearm(command, ACTOR)
    assert isinstance(cancelled.snapshot.intent, CancelledRearm)
    replay = workflow.cancel_rearm(command, replace(ACTOR, channel="operator_cli"))
    assert replay.idempotent and replay.snapshot == cancelled.snapshot

    changed = replace(command, reason="different reason")
    assert isinstance(workflow.cancel_rearm(changed, ACTOR), WorkflowConflict)
    other_actor = OperatorActor("operator:bob", "dashboard")
    assert isinstance(workflow.cancel_rearm(command, other_actor), WorkflowConflict)


def test_generic_cancellation_cannot_discard_a_successful_required_receipt(control):
    _, proposal, pending = _pending(control)
    command_id, receipt = _succeed_required_projection(control, proposal, pending)
    command = CancelRearm(
        "cancel:with-success", pending.revision, pending.intent.request_id,
        "operator cancelled",
    )
    forged = cancel_resolve_rearm(pending, command, ACTOR)

    with pytest.raises(WorkflowAuthorityError, match="retain every successful"):
        commit(control, forged)

    workflow = adapter(control)
    assert workflow.load_workflow(pending.revision.fence.key) == pending
    assert control.effect_receipt(command_id) == receipt
    assert command_row(control, command_id)["state"] == EffectStatus.SUCCEEDED.value

    cancelled = workflow.cancel_rearm(command, ACTOR)
    assert isinstance(cancelled.snapshot.intent, CancelledRearm)
    assert cancelled.snapshot.intent.verified_commands == (command_id,)
    assert control.effect_receipt(command_id) == receipt


def test_generic_terminal_supersession_cannot_discard_a_successful_required_receipt(
    control,
):
    _, proposal, pending = _pending(control)
    command_id, receipt = _succeed_required_projection(control, proposal, pending)
    closed = replace(
        observation(pending), issue=replace(observation(pending).issue, state="closed"),
    )
    evidence(control, pending, "closed:with-success", closed)
    forged = supersede_resolve_rearm(
        pending, StopReason.TARGET_TERMINAL, "closed:with-success",
    )

    with pytest.raises(WorkflowAuthorityError, match="retain every successful"):
        commit(control, forged)

    assert adapter(control).load_workflow(pending.revision.fence.key) == pending
    assert control.effect_receipt(command_id) == receipt
    assert command_row(control, command_id)["state"] == EffectStatus.SUCCEEDED.value
