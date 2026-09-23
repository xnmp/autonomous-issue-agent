"""Operator projection and command composition; no legacy unblock fallback."""

import os

from ...domain import workflow as w
from ...domain.resolve_rearm import AbsentResolveControl, InactiveResolveControl, ResolveControlView
from ...domain.workflow_identity import IssueRef, WorkflowKey
from ..workflows.resolve_rearm import ResolveRearmWorkflow
from . import resolve_contracts as wire


def _rearm(intent: w.RearmIntent | None) -> wire.Rearm | None:
    if intent is None:
        return None
    handoff = intent.handoff
    state = ("pending" if isinstance(intent, w.PendingRearm) else
             "completed" if isinstance(intent, w.CompletedRearm) else
             "cancelled" if isinstance(intent, w.CancelledRearm) else "superseded")
    reason = (intent.reason.value if isinstance(intent, w.SupersededRearm) else
              intent.reason if isinstance(intent, w.CancelledRearm) else "")
    return wire.Rearm(
        request_id=intent.request_id, state=state, gate_id=intent.gate_id, generation=intent.to_generation,
        grant_request_id=handoff.grant_request_id, episode_id=handoff.destination.episode_id,
        handoff_id=handoff.handoff_id, branch=handoff.branch, base_ref=handoff.base.base_ref,
        base_commit=handoff.base.commit_sha,
        required_commands=list(intent.required_commands) if isinstance(intent, w.PendingRearm) else [],
        verified_commands=list(intent.verified_commands) if not isinstance(intent, w.PendingRearm) else [],
        retired_commands=list(intent.retired_obligations) if isinstance(intent, (w.CancelledRearm, w.SupersededRearm)) else [],
        reason=reason,
    )


def _cause(cause: w.GateCause) -> wire.GateCause:
    if isinstance(cause, w.ResolveCeiling):
        return wire.ResolveCeiling(kind="resolve_ceiling", episode_id=cause.episode_id,
                                   failures=cause.failures, ceiling=cause.ceiling, last_failure=cause.last_failure)
    if isinstance(cause, w.RemoteOutcomeUncertain):
        return wire.RemoteUncertain(kind="remote_uncertain", command_id=cause.command_id,
                                    unresolved_since=cause.unresolved_since.isoformat(), failed_reads=cause.failed_reads,
                                    last_error=cause.last_error)
    if isinstance(cause, w.RetryAuthorizationRequired):
        return wire.RetryAuthorizationRequired(kind="retry_authorization_required", command_id=cause.command_id,
                                               absence_evidence_id=cause.absence_evidence_id)
    if isinstance(cause, w.EvidenceUnavailable):
        return wire.EvidenceUnavailable(kind="evidence_unavailable", source=cause.source,
                                        unresolved_since=cause.unresolved_since.isoformat(), failed_reads=cause.failed_reads,
                                        last_error=cause.last_error)
    return wire.RearmNeedsDecision(kind="rearm_needs_decision", stopped_request_id=cause.stopped_request_id,
                                   obligations=list(cause.obligations), reason=cause.reason.value)


def _gate(gate: w.UnresolvedGate, snapshot: w.WorkflowSnapshot) -> wire.Gate:
    opened = gate.gate if isinstance(gate, w.RearmingGate) else gate
    remediation = opened.remediation
    remedy = (wire.NoRemediation(kind="none_authorized", reason=remediation.reason)
              if isinstance(remediation, w.NoAuthorizedRemediation) else
              wire.AttemptedRemediation(kind="attempted", operation=remediation.operation.value,
                                        evidence_ids=list(remediation.evidence_ids), outcome=remediation.outcome.value))
    commands = []
    intent = snapshot.intent
    if isinstance(intent, w.PendingRearm) and intent.gate_id == opened.gate_id:
        commands = ["cancel_rearm"]
    elif isinstance(intent, (w.CancelledRearm, w.SupersededRearm)) and intent.gate_id == opened.gate_id:
        commands = ["continue_rearm"]
    elif isinstance(opened.cause, w.ResolveCeiling) and not isinstance(intent, w.PendingRearm):
        commands = ["request_rearm"]
    return wire.Gate(gate_id=opened.gate_id, state="rearming" if isinstance(gate, w.RearmingGate) else "open",
                     opened_generation=opened.opened_generation, cause=_cause(opened.cause),
                     remediation=remedy, available_commands=commands)


def read_control(workflow: ResolveRearmWorkflow, number: int, request_id: str | None = None) -> wire.ResolveControl:
    key = WorkflowKey(workflow.control.writer.repository, IssueRef(number))
    result = workflow.control.resolve_control_view(key, request_id=request_id)
    if isinstance(result, w.WorkflowUnavailable):
        return wire.UnavailableControl(status="unavailable", reason=result.reason)
    if isinstance(result, InactiveResolveControl):
        return wire.InactiveControl(status="inactive", repository=key.repository, issue=number)
    if isinstance(result, AbsentResolveControl):
        return wire.AbsentControl(status="absent", repository=key.repository, issue=number, epoch=result.authority.epoch)
    assert isinstance(result, ResolveControlView)
    state = result.snapshot
    observation = result.observation
    return wire.AvailableControl(
        status="available",
        expected=wire.ResolveExpected(repository=key.repository, issue=number, family="resolve", epoch=state.authority.epoch,
                                      generation=state.revision.fence.generation, version=state.revision.version,
                                      episode_id=state.episode.episode_id),
        workflow_held=bool(state.gates), gates=[_gate(gate, state) for gate in state.gates],
        guidance=state.episode.guidance, failures=state.episode.failures,
        current_intent=_rearm(state.intent), requested_id=result.requested_id, requested_intent=_rearm(result.requested),
        observation=(wire.Observation(unresolved_since=observation.history.unresolved_since.isoformat(),
                                      next_read_at=observation.history.next_read_at.isoformat(),
                                      failed_reads=observation.history.failed_reads, last_error=observation.last_error)
                     if observation is not None else None),
    )


def submit_control(workflow: ResolveRearmWorkflow, number: int, body: wire.ResolveCommand) -> wire.CommandResult:
    command = body.command()
    key = WorkflowKey(workflow.control.writer.repository, IssueRef(number))
    if body.expected.binding().fence.key != key:
        return wire.CommandResult(outcome="rejected", reason="command target differs from the configured deployment",
                                  control=read_control(workflow, number))
    # This subject names the trusted local operator account, not an HTTP user
    # or User-Agent claim. The server's loopback/Origin/CSRF boundary is required.
    actor = w.OperatorActor(f"local-user:{os.getuid()}", "dashboard")
    result = workflow.submit(command, actor, binding=body.expected.binding())
    control = read_control(workflow, number, body.request_id)
    if isinstance(result, w.WorkflowCommitted):
        return wire.CommandResult(outcome="replayed" if result.idempotent else "accepted", reason="", control=control)
    if isinstance(result, w.WorkflowConflict):
        return wire.CommandResult(outcome="conflict", reason=result.reason, control=control)
    return wire.CommandResult(outcome="unavailable" if result.code is w.WorkflowRejection.OBSERVATION_UNAVAILABLE else "rejected",
                              reason=result.reason, control=control)
