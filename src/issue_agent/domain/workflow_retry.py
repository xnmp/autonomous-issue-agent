"""One-attempt retry authorization after verified absence; ADR 0015.

An authenticated operator may authorize exactly one more execution of an
existing reconciliation-only command whose current postcondition the harness
proved absent. The authorization grants no allowance, creates no re-arm,
advances no generation and resets no unresolved history.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .contracts import CommandId
from .workflow import (
    AuthorizeAbsentRetry, AuthorizedOperatorCommand, CommandAbsenceEvidence, CommandAssociation,
    CommandReconciliation, ConsumedRetryAuthorization, OpenGate, OperatorActor, PendingRearm,
    RearmObligation, ReconciliationOnly, RetiredSettledCommand, RetiredUncertainCommand,
    RetryAuthorizationRequired, StandaloneCommand, UnconsumedRetryAuthorization, WorkflowChanged,
    WorkflowMutation, WorkflowRejected, WorkflowRejection, WorkflowSnapshot, WorkflowTransition,
    ActiveReconciliation, ClosedRetryAuthorization, RearmingGate, RetryDisposition,
)
from .review_workflow import PendingReviewRearm, ReviewWorkflowSnapshot
from .workflow_reconciliation import ABSENCE_FRESHNESS_SECONDS
from .workflow_identity import WorkflowBinding, WorkflowRevision, validate_workflow_identifier

MAX_RELEVANT_AUTHORIZATIONS = 64
# Human-gated bound: each grant buys at most one attempt, and one command can
# be granted at most this many attempts over its life (ADR 0015).
MAX_AUTHORIZED_ATTEMPTS = 3
# An open grant survives transient read failures under the normal backoff,
# but only this many of them, and only for this long, before it is voided.
MAX_GRANT_UNAVAILABLE_READS = 5
MAX_GRANT_AGE_SECONDS = 86400

RetryAuthorization = UnconsumedRetryAuthorization | ConsumedRetryAuthorization | ClosedRetryAuthorization
# Both families share one grant contract: the same scope, evidence, ceiling,
# disposition and one-write rules (ADR 0015 review slice, increment 5).
RetrySnapshot = WorkflowSnapshot | ReviewWorkflowSnapshot
_SNAPSHOTS = (WorkflowSnapshot, ReviewWorkflowSnapshot)


def _authorization(value: RetryAuthorization) -> UnconsumedRetryAuthorization:
    return value if isinstance(value, UnconsumedRetryAuthorization) else value.authorization


def _aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class RetryAuthorizationFacts:
    """Harness-owned command facts read inside the deciding transaction.

    Provenance: the owning role's control store mints every field from its own
    rows (command outbox, workflow_evidence kind 'absence', re-arm linkage,
    workspace acknowledgement, paid-return provenance and prior grants). Wire
    input never supplies any of them. Acceptance, the operator view and the
    executor's spend decision all read these same facts.
    """
    command_id: CommandId
    command_binding: WorkflowBinding
    reconciliation: CommandReconciliation | None
    evidence: CommandAbsenceEvidence | None
    association: CommandAssociation
    prior: tuple[RetryAuthorization, ...] = ()
    # Returned-result projections execute only on an unheld generation.
    returns_resolve_result: bool = False
    # Re-arm linkage still 'required' and its workspace handoff acknowledged.
    obligation_active: bool = False
    workspace_ready: bool = False
    # Start of the latest authorized attempt; absence must postdate it.
    last_attempt_at: datetime | None = None
    # Attempts already bought by consumed grants for this command.
    authorized_attempts: int = 0
    # Why the store's own execution-authority check would refuse a begin now.
    execution_refusal: str | None = None

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "retry command")
        if not isinstance(self.command_binding, WorkflowBinding):
            raise ValueError("retry facts require the command's captured binding")
        if self.reconciliation is not None and not isinstance(
            self.reconciliation,
            (ActiveReconciliation, ReconciliationOnly, RetiredUncertainCommand, RetiredSettledCommand),
        ):
            raise ValueError("retry facts require a typed reconciliation state")
        if self.evidence is not None and (
            not isinstance(self.evidence, CommandAbsenceEvidence) or self.evidence.command_id != self.command_id
        ):
            raise ValueError("absence evidence must belong to the queried command")
        if not isinstance(self.association, (StandaloneCommand, RearmObligation)):
            raise ValueError("retry facts require a typed command association")
        if not isinstance(self.prior, tuple) or len(self.prior) > MAX_RELEVANT_AUTHORIZATIONS or any(
            not isinstance(item, (UnconsumedRetryAuthorization, ConsumedRetryAuthorization, ClosedRetryAuthorization))
            or _authorization(item).command_id != self.command_id
            for item in self.prior
        ):
            raise ValueError("prior authorizations must be a bounded tuple for this command")
        for name in ("returns_resolve_result", "obligation_active", "workspace_ready"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean fact")
        if self.last_attempt_at is not None:
            _aware(self.last_attempt_at, "last authorized attempt")
        if type(self.authorized_attempts) is not int or self.authorized_attempts < 0:
            raise ValueError("authorized attempt count must be a non-negative integer")
        if self.execution_refusal is not None and (
            not isinstance(self.execution_refusal, str) or not self.execution_refusal
        ):
            raise ValueError("an execution refusal must state its reason")


def _reject(code: WorkflowRejection, reason: str) -> WorkflowRejected:
    return WorkflowRejected(code, reason)


def _scope_refusal(
    snapshot: RetrySnapshot, command_id: CommandId, gate_id: str, facts: RetryAuthorizationFacts,
) -> str | None:
    """The one scope contract shared by acceptance, the view and execution.

    Everything that could stop the executor from beginning the attempt, other
    than the remote read itself, is refused here so an operator is never
    offered a grant the executor would have to discard.
    """
    gate = next((g for g in snapshot.gates if isinstance(g, OpenGate) and g.gate_id == gate_id), None)
    if gate is None or not isinstance(gate.cause, RetryAuthorizationRequired):
        return "the named gate does not require a retry authorization"
    if gate.cause.command_id != command_id:
        return "the gate names another command or a newer absence observation"
    state = facts.reconciliation
    if isinstance(state, (RetiredUncertainCommand, RetiredSettledCommand)):
        return "a retired command can never be authorized to execute"
    if not isinstance(state, ReconciliationOnly) or state.gate_id != gate_id:
        return "the command is not reconciliation-only under this gate"
    if facts.command_binding != snapshot.binding:
        return "the command belongs to another workflow generation, epoch or episode"
    intent = snapshot.intent
    pending = isinstance(intent, (PendingRearm, PendingReviewRearm))
    required = intent.required_commands if isinstance(intent, (PendingRearm, PendingReviewRearm)) else ()
    if isinstance(facts.association, RearmObligation):
        if (not pending or intent.request_id != facts.association.request_id
                or command_id not in required or not facts.obligation_active):
            return "the re-arm request owning this obligation is no longer pending"
        if not facts.workspace_ready:
            return "the re-arm obligation awaits its verified workspace handoff"
    elif command_id in required:
        return "a pending re-arm obligation lacks its recorded association"
    elif facts.returns_resolve_result and any(
        (item.gate if isinstance(item, RearmingGate) else item).gate_id != gate_id for item in snapshot.gates
    ):
        return "another gate holds the returned completion"
    if facts.execution_refusal is not None:
        return f"workflow authority refuses execution: {facts.execution_refusal}"
    return None


def retry_authorization_refusal(
    snapshot: RetrySnapshot, *, command_id: CommandId, gate_id: str, evidence_id: str,
    facts: RetryAuthorizationFacts,
) -> WorkflowRejected | None:
    """Every acceptance precondition except revision/identity replay."""
    if not isinstance(snapshot, _SNAPSHOTS) or not isinstance(facts, RetryAuthorizationFacts):
        raise ValueError("retry authorization policy requires typed state and facts")
    if facts.command_id != command_id:
        raise ValueError("retry facts were read for another command")
    if any(isinstance(item, UnconsumedRetryAuthorization) for item in facts.prior):
        return _reject(WorkflowRejection.AUTHORIZATION_OUTSTANDING,
                       "this command already has an unconsumed retry authorization")
    scope = _scope_refusal(snapshot, command_id, gate_id, facts)
    if scope is not None:
        return _reject(WorkflowRejection.INVALID_SCOPE, scope)
    gate = next(g for g in snapshot.gates if isinstance(g, OpenGate) and g.gate_id == gate_id)
    assert isinstance(gate.cause, RetryAuthorizationRequired) and isinstance(facts.reconciliation, ReconciliationOnly)
    if gate.cause.absence_evidence_id != evidence_id:
        return _reject(WorkflowRejection.INVALID_SCOPE,
                       "the gate names another command or a newer absence observation")
    evidence = facts.evidence
    if evidence is None or evidence.evidence_id != evidence_id or evidence.binding != snapshot.binding:
        return _reject(WorkflowRejection.INVALID_SCOPE,
                       "the evidence is not the harness's own absence observation for this command")
    if evidence.observed_at < facts.reconciliation.history.unresolved_since:
        return _reject(WorkflowRejection.INVALID_SCOPE, "the absence observation predates the command's uncertainty")
    if facts.last_attempt_at is not None and evidence.observed_at <= facts.last_attempt_at:
        return _reject(WorkflowRejection.INVALID_SCOPE,
                       "the absence observation predates the last authorized attempt")
    if facts.authorized_attempts >= MAX_AUTHORIZED_ATTEMPTS:
        return _reject(WorkflowRejection.INVALID_SCOPE,
                       f"this command already used its {MAX_AUTHORIZED_ATTEMPTS} authorized attempts")
    backed = next((item for item in facts.prior if _authorization(item).evidence_id == evidence_id), None)
    if backed is not None:
        return _reject(WorkflowRejection.INVALID_SCOPE, (
            "this absence observation already bought an attempt" if isinstance(backed, ConsumedRetryAuthorization)
            else "this absence observation already backed a grant that ended unspent"
        ) + "; await a new verified absence")
    if any(isinstance(item, ClosedRetryAuthorization) and item.disposition is RetryDisposition.INAPPLICABLE
           for item in facts.prior):
        return _reject(WorkflowRejection.INVALID_SCOPE,
                       "the original decision was judged inapplicable; this command can never execute again")
    return None


def retry_authorization_available(
    snapshot: RetrySnapshot, gate: OpenGate, facts: RetryAuthorizationFacts,
) -> bool:
    """The operator view offers exactly what acceptance would admit now."""
    if not isinstance(gate, OpenGate) or not isinstance(gate.cause, RetryAuthorizationRequired):
        return False
    return retry_authorization_refusal(
        snapshot, command_id=gate.cause.command_id, gate_id=gate.gate_id,
        evidence_id=gate.cause.absence_evidence_id, facts=facts,
    ) is None


@dataclass(frozen=True)
class RetryAbsenceRead:
    """The executor's own just-completed read (harness executor trust domain).

    `observed_at` is taken on the control store's clock after both the
    postcondition read (absent) and the target revision read completed;
    `guard_holds` is the executor's re-evaluation of the original decision's
    own facts (ADR 0015 semantic fence), the only applicability authority.
    `target_revision` is recorded as evidence; a raw revision change such as a
    comment is not by itself a changed decision.
    """
    observed_at: datetime
    target_revision: str
    guard_holds: bool

    def __post_init__(self) -> None:
        _aware(self.observed_at, "absence read time")
        validate_workflow_identifier(self.target_revision, "observed target revision")
        if not isinstance(self.guard_holds, bool):
            raise ValueError("guard evaluation must be a boolean")


@dataclass(frozen=True)
class BeginRetry:
    pass


@dataclass(frozen=True)
class DeclineRetry:
    """Terminal grant disposition; `fresh_read` says whether the read may back new absence evidence."""
    disposition: RetryDisposition
    reason: str
    fresh_read: bool

    def __post_init__(self) -> None:
        if self.disposition not in (RetryDisposition.INAPPLICABLE, RetryDisposition.VOID):
            raise ValueError("a spend decision declines only as inapplicable or void")


def retry_spend_decision(
    snapshot: RetrySnapshot, grant: UnconsumedRetryAuthorization, facts: RetryAuthorizationFacts,
    read: RetryAbsenceRead, *, now: datetime, effect_owed: bool = True,
) -> BeginRetry | DeclineRetry:
    """Decide, once, what an open grant buys after the executor proved absence.

    `effect_owed` is the owning store's own fact, read in the spend's
    transaction, that the workflow still owes this effect (a review family
    submission whose review already landed is not). Every non-begin outcome is
    terminal for the grant, so no refusal can loop.
    """
    if not isinstance(effect_owed, bool):
        raise ValueError("retry spend requires a typed owed-effect fact")
    if not isinstance(snapshot, _SNAPSHOTS) or not isinstance(grant, UnconsumedRetryAuthorization) or not isinstance(
        facts, RetryAuthorizationFacts,
    ) or not isinstance(read, RetryAbsenceRead):
        raise ValueError("retry spend requires typed state, grant, facts and read")
    _aware(now, "spend decision clock")
    if grant.command_id != facts.command_id:
        raise ValueError("retry facts were read for another command")
    state = facts.reconciliation
    at = read.observed_at
    fresh = (
        0 <= (now - at).total_seconds() <= ABSENCE_FRESHNESS_SECONDS
        and (not isinstance(state, ReconciliationOnly) or at >= state.history.unresolved_since)
        and (facts.evidence is None or at >= facts.evidence.observed_at)
        and (facts.last_attempt_at is None or at > facts.last_attempt_at)
    )
    if not fresh:
        return DeclineRetry(RetryDisposition.VOID, "the executor's absence read is not fresh", False)
    scope = _scope_refusal(snapshot, grant.command_id, grant.gate_id, facts)
    if scope is not None:
        return DeclineRetry(RetryDisposition.VOID, scope, True)
    gate = next(g for g in snapshot.gates if isinstance(g, OpenGate) and g.gate_id == grant.gate_id)
    assert isinstance(gate.cause, RetryAuthorizationRequired)
    if gate.cause.absence_evidence_id != grant.evidence_id:
        return DeclineRetry(RetryDisposition.VOID, "a newer observation superseded the grant's absence evidence", True)
    if grant.binding != snapshot.binding:
        return DeclineRetry(RetryDisposition.VOID, "the grant belongs to another workflow generation, epoch or episode", True)
    if not effect_owed:
        return DeclineRetry(RetryDisposition.INAPPLICABLE, "the workflow no longer owes this effect", True)
    if not read.guard_holds:
        return DeclineRetry(
            RetryDisposition.INAPPLICABLE,
            f"original decision guard no longer holds (observed revision {read.target_revision!r})",
            True,
        )
    return BeginRetry()


def authorize_absent_retry(
    snapshot: RetrySnapshot, command: AuthorizeAbsentRetry, actor: OperatorActor,
    facts: RetryAuthorizationFacts,
) -> WorkflowTransition:
    if not isinstance(command, AuthorizeAbsentRetry) or not isinstance(actor, OperatorActor):
        raise ValueError("retry authorization requires a typed command and authenticated actor")
    if command.expected != snapshot.revision:
        return _reject(WorkflowRejection.STALE_REVISION, "workflow changed before retry authorization")
    refusal = retry_authorization_refusal(
        snapshot, command_id=command.command_id, gate_id=command.gate_id,
        evidence_id=command.evidence_id, facts=facts,
    )
    if refusal is not None:
        return refusal
    current = snapshot.revision
    after = replace(snapshot, revision=WorkflowRevision(current.fence, current.version + 1))
    # A review transition carries its exact prior as the witness the commit
    # adapter proves equal to the stored row; resolve keeps none.
    return WorkflowChanged(
        current, WorkflowMutation.AUTHORIZE_RETRY, after, AuthorizedOperatorCommand(actor, command),
        prior=snapshot if isinstance(snapshot, ReviewWorkflowSnapshot) else None,
    )


def minted_authorization(command: AuthorizeAbsentRetry, facts: RetryAuthorizationFacts) -> UnconsumedRetryAuthorization:
    """The durable grant an accepted command persists; binding is the command's own."""
    return UnconsumedRetryAuthorization(
        command.authorization_id, command.command_id, command.gate_id, command.evidence_id,
        facts.command_binding, facts.association,
    )


def grant_unavailable_void(unavailable_reads: int, accepted_at: datetime, now: datetime) -> str | None:
    """Why an open grant ends after another unavailable read, or None to keep it."""
    _aware(accepted_at, "grant acceptance")
    _aware(now, "grant clock")
    if type(unavailable_reads) is not int or unavailable_reads < 0:
        raise ValueError("unavailable read count must be a non-negative integer")
    if unavailable_reads >= MAX_GRANT_UNAVAILABLE_READS:
        return f"the grant saw {unavailable_reads} unavailable reads; its ceiling is {MAX_GRANT_UNAVAILABLE_READS}"
    if (now - accepted_at).total_seconds() >= MAX_GRANT_AGE_SECONDS:
        return "the grant outlived its bounded age without a complete read"
    return None
