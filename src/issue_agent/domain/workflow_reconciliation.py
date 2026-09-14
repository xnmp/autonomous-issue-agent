"""Bounded reconciliation scheduling without inferring remote absence; ADR 0015."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib

from .contracts import CommandId
from .workflow import (
    ActiveReconciliation, CommandReconciliation, OpenGate, ReconciliationOnly,
    ReconciliationPolicy, RemediationAttempted, RemediationOperation,
    RemediationOutcome, RemoteOutcomeUncertain, RetiredSettledCommand,
    RetiredUncertainCommand,
)
from .workflow_identity import WorkflowBinding, validate_workflow_identifier


@dataclass(frozen=True)
class ReconciliationProgress:
    state: CommandReconciliation
    gate: OpenGate | None


def reconciliation_due(state: CommandReconciliation, now: datetime) -> bool:
    if isinstance(state, RetiredSettledCommand):
        return False
    history = state.uncertainty.history if isinstance(state, RetiredUncertainCommand) else (
        state.history if isinstance(state, ReconciliationOnly) else state
    )
    return now >= history.next_read_at


def unavailable_reconciliation(
    binding: WorkflowBinding,
    command_id: CommandId,
    prior: CommandReconciliation | None,
    *,
    now: datetime,
    evidence_id: str,
    error: str,
    policy: ReconciliationPolicy = ReconciliationPolicy(),
) -> ReconciliationProgress:
    """Record an actual failed read; expiry can only remove execution permission."""
    validate_workflow_identifier(command_id, "command")
    validate_workflow_identifier(evidence_id, "reconciliation evidence")
    if not isinstance(binding, WorkflowBinding) or not isinstance(policy, ReconciliationPolicy):
        raise ValueError("reconciliation requires validated authority and policy")
    if isinstance(prior, RetiredSettledCommand):
        return ReconciliationProgress(prior, None)
    if prior is not None and not isinstance(prior, (ActiveReconciliation, ReconciliationOnly, RetiredUncertainCommand)):
        raise ValueError("reconciliation history must be a validated variant")
    previous = prior.uncertainty if isinstance(prior, RetiredUncertainCommand) else prior
    old_history = previous.history if isinstance(previous, ReconciliationOnly) else previous
    since = old_history.unresolved_since if old_history is not None else now
    reads = min(old_history.failed_reads + 1, 2**63 - 1) if old_history is not None else 1
    delay = min(policy.maximum_delay_seconds, policy.initial_delay_seconds * 2 ** min(reads - 1, 17))
    history = ActiveReconciliation(since, now + timedelta(seconds=delay), reads)
    if now < since:
        raise ValueError("reconciliation observation precedes unresolved history")
    expired = isinstance(previous, ReconciliationOnly) or reads >= policy.maximum_reads or (
        now - since
    ).total_seconds() >= policy.maximum_age_seconds
    if not expired:
        return ReconciliationProgress(history, None)
    gate_id = previous.gate_id if isinstance(previous, ReconciliationOnly) else hashlib.sha256(
        f"workflow-uncertainty:{command_id}".encode()
    ).hexdigest()
    uncertainty = ReconciliationOnly(history, gate_id)
    state = RetiredUncertainCommand(uncertainty, prior.retirement_evidence_id) if isinstance(prior, RetiredUncertainCommand) else uncertainty
    return ReconciliationProgress(
        state,
        OpenGate(
            gate_id, binding.fence.generation,
            RemoteOutcomeUncertain(command_id, since, reads, error),
            RemediationAttempted(RemediationOperation.RECONCILE_REMOTE, (evidence_id,), RemediationOutcome.UNKNOWN),
        ),
    )
