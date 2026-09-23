"""Observation scheduling attached to existing re-arm intent, never a work queue."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib

from .contracts import CommandId
from .labels import Label
from .resolve_workspace import EpisodeWorkspacePrepared
from .workflow import (
    ActiveReconciliation, EvidenceUnavailable, LinkedPrPresent, NoAuthorizedRemediation,
    NoLinkedPr, ObservedIssue, OpenGate, PendingRearm, RearmingGate, ReconciliationPolicy,
    RemoteOutcomeUncertain, ResolveRearmAcceptance, ResolveRearmPreflight,
    StopReason, WorkflowSnapshot, WorkflowTransition, WorkflowUnchanged,
    RearmIntent, WorkflowUnavailable,
)
from .workflow_identity import FamilyAuthority, WorkflowBinding, WorkflowKey, validate_workflow_identifier, validate_workflow_text
from .workflow_policy import complete_resolve_rearm, supersede_resolve_rearm


@dataclass(frozen=True)
class RearmObservation:
    request_id: str
    binding: WorkflowBinding
    history: ActiveReconciliation
    last_error: str = ""

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.request_id, "rearm observation request")
        if not isinstance(self.binding, WorkflowBinding) or not isinstance(self.history, ActiveReconciliation):
            raise ValueError("rearm observation requires captured authority and read history")
        validate_workflow_text(self.last_error, "rearm observation error", limit=16384, empty=True)
        if bool(self.last_error) != bool(self.history.failed_reads):
            raise ValueError("failed rearm reads must retain their error, successful reads must not")


@dataclass(frozen=True)
class PendingResolveRearm:
    snapshot: WorkflowSnapshot
    observation: RearmObservation

    def __post_init__(self) -> None:
        if (not isinstance(self.snapshot, WorkflowSnapshot)
                or not isinstance(self.snapshot.intent, PendingRearm)
                or not isinstance(self.observation, RearmObservation)
                or self.observation.request_id != self.snapshot.intent.request_id
                or self.observation.binding != self.snapshot.binding):
            raise ValueError("pending recovery requires its exact rearm observation authority")


@dataclass(frozen=True)
class UnavailableResolveRearm:
    request_id: str
    reason: str
    next_read_at: datetime

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.request_id, "unavailable rearm request")
        validate_workflow_text(self.reason, "unavailable rearm reason", limit=16384)
        if not isinstance(self.next_read_at, datetime) or self.next_read_at.utcoffset() is None:
            raise ValueError("unavailable rearm requires an aware next-read time")


@dataclass(frozen=True)
class InactiveResolveControl:
    key: WorkflowKey

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey):
            raise ValueError("inactive resolve control requires a queried workflow")


@dataclass(frozen=True)
class AbsentResolveControl:
    key: WorkflowKey
    authority: FamilyAuthority

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey) or not isinstance(self.authority, FamilyAuthority):
            raise ValueError("absent resolve control requires its active family authority")


@dataclass(frozen=True)
class ResolveControlView:
    snapshot: WorkflowSnapshot
    requested_id: str | None
    requested: RearmIntent | None
    observation: RearmObservation | None

    def __post_init__(self) -> None:
        from .workflow import CancelledRearm, CompletedRearm, SupersededRearm

        if not isinstance(self.snapshot, WorkflowSnapshot):
            raise ValueError("resolve control view requires current workflow authority")
        if self.requested_id is not None:
            validate_workflow_identifier(self.requested_id, "requested rearm")
        if self.requested is not None and (
            not isinstance(self.requested, (PendingRearm, CompletedRearm, CancelledRearm, SupersededRearm))
            or self.requested.request_id != self.requested_id
            or self.requested.handoff.destination.fence.key != self.snapshot.binding.fence.key
        ):
            raise ValueError("requested rearm must belong to this exact workflow and request")
        if isinstance(self.snapshot.intent, PendingRearm):
            if self.observation is None:
                raise ValueError("pending resolve control requires its observation history")
            PendingResolveRearm(self.snapshot, self.observation)
        elif self.observation is not None:
            raise ValueError("only current pending control carries observation history")


ResolveControlRead = InactiveResolveControl | AbsentResolveControl | ResolveControlView | WorkflowUnavailable


def _clock(previous: RearmObservation, now: datetime) -> None:
    if not isinstance(previous, RearmObservation) or not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("rearm scheduling requires validated history and an aware clock")
    if now < previous.history.unresolved_since:
        raise ValueError("rearm observation precedes its retained history")


def defer_rearm_observation(
    previous: RearmObservation, error: str, *, now: datetime,
    policy: ReconciliationPolicy = ReconciliationPolicy(),
) -> RearmObservation:
    _clock(previous, now)
    validate_workflow_text(error, "rearm observation failure", limit=16384)
    if not isinstance(policy, ReconciliationPolicy):
        raise ValueError("rearm scheduling requires a validated retry policy")
    reads = min(previous.history.failed_reads + 1, 2**63 - 1)
    delay = min(policy.maximum_delay_seconds, policy.initial_delay_seconds * 2 ** min(reads - 1, 17))
    since = previous.history.unresolved_since if previous.last_error else now
    history = ActiveReconciliation(since, max(previous.history.next_read_at, now + timedelta(seconds=delay)), reads)
    return replace(previous, history=history, last_error=error)


def schedule_rearm_observation(
    previous: RearmObservation, *, now: datetime,
    policy: ReconciliationPolicy = ReconciliationPolicy(),
) -> RearmObservation:
    _clock(previous, now)
    if not isinstance(policy, ReconciliationPolicy):
        raise ValueError("rearm scheduling requires a validated retry policy")
    return replace(previous, history=ActiveReconciliation(now, now + timedelta(seconds=policy.initial_delay_seconds), 0), last_error="")


def update_rearm_observation(
    snapshot: WorkflowSnapshot, observation: RearmObservation | None,
    *, now: datetime, policy: ReconciliationPolicy = ReconciliationPolicy(),
) -> WorkflowSnapshot:
    """Only a failed bounded read adds a hold; successful recovery removes its own."""
    if not isinstance(snapshot, WorkflowSnapshot) or not isinstance(snapshot.intent, PendingRearm):
        raise ValueError("rearm observation requires current pending intent")
    if not isinstance(policy, ReconciliationPolicy):
        raise ValueError("rearm observation requires a validated retry policy")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("rearm observation requires an aware clock")
    if observation is not None and (not isinstance(observation, RearmObservation)
            or observation.request_id != snapshot.intent.request_id or observation.binding != snapshot.binding):
        raise ValueError("rearm observation belongs to another intent or authority")
    # Continue inherits the same workspace obligation; a later successful
    # verification must also retire that obligation's earlier observation hold.
    source = "rearm:" + snapshot.intent.handoff.handoff_id
    gate_id = "rearm-observation:" + hashlib.sha256(source.encode()).hexdigest()
    prior_gate = next((g for g in snapshot.gates if isinstance(g, OpenGate) and g.gate_id == gate_id), None)
    gates = tuple(g for g in snapshot.gates if (g.gate.gate_id if isinstance(g, RearmingGate) else g.gate_id) != gate_id)
    if observation is not None and observation.last_error:
        history = observation.history
        if now < history.unresolved_since:
            raise ValueError("rearm observation precedes retained history")
        if prior_gate is not None or history.failed_reads >= policy.maximum_reads or (now - history.unresolved_since).total_seconds() >= policy.maximum_age_seconds:
            gates += (OpenGate(gate_id, prior_gate.opened_generation if prior_gate else snapshot.binding.fence.generation,
                              EvidenceUnavailable(source, history.unresolved_since, history.failed_reads, observation.last_error),
                              NoAuthorizedRemediation("fresh re-arm observations and workspace verification are required")),)
    if gates == snapshot.gates:
        return snapshot
    return replace(snapshot, gates=gates, revision=replace(snapshot.revision, version=snapshot.revision.version + 1))


def decide_rearm_reconciliation(
    snapshot: WorkflowSnapshot,
    accepted_preflight: ResolveRearmPreflight | ResolveRearmAcceptance,
    issue: ObservedIssue, linkage: NoLinkedPr | LinkedPrPresent, *,
    verified_commands: tuple[CommandId, ...], rejected_commands: tuple[CommandId, ...],
    uncertainty: tuple[RemoteOutcomeUncertain, ...], workspace: EpisodeWorkspacePrepared | None,
    evidence_id: str, now: datetime, external_holds: tuple[str, ...] = (),
) -> WorkflowTransition:
    """Select from observed facts without treating waiting as completion."""
    if not isinstance(snapshot, WorkflowSnapshot) or not isinstance(accepted_preflight, (ResolveRearmPreflight, ResolveRearmAcceptance)):
        raise ValueError("rearm reconciliation requires snapshot and accepted decision facts")
    if not isinstance(issue, ObservedIssue) or not isinstance(linkage, (NoLinkedPr, LinkedPrPresent)):
        raise ValueError("rearm reconciliation requires complete remote observations")
    key = snapshot.revision.fence.key
    if issue.key != key or linkage.issue != key or accepted_preflight.issue.key != key:
        raise ValueError("rearm reconciliation observations belong to another workflow")
    if not isinstance(now, datetime) or now.utcoffset() is None or any(
        not 0 <= (now - observed.observed_at).total_seconds() <= 300 for observed in (issue, linkage)
    ):
        raise ValueError("rearm observations must be fresh and not from the future")
    validate_workflow_identifier(evidence_id, "rearm observation evidence")
    if not isinstance(snapshot.intent, PendingRearm):
        return WorkflowUnchanged(snapshot)
    required = set(snapshot.intent.required_commands)
    for group in (verified_commands, rejected_commands):
        if not isinstance(group, tuple) or len(set(group)) != len(group) or not set(group) <= required:
            raise ValueError("receipt evidence must name unique required rearm commands")
    if set(verified_commands) & set(rejected_commands):
        raise ValueError("required command cannot be both verified and rejected")
    if not isinstance(uncertainty, tuple) or any(not isinstance(item, RemoteOutcomeUncertain) for item in uncertainty):
        raise ValueError("rearm uncertainty requires typed command observations")
    uncertain_commands = tuple(item.command_id for item in uncertainty)
    if (len(set(uncertain_commands)) != len(uncertain_commands)
            or not set(uncertain_commands) <= required
            or set(uncertain_commands) & (set(verified_commands) | set(rejected_commands))):
        raise ValueError("rearm uncertainty must name unique unresolved required commands")
    accepted = accepted_preflight.issue.issue
    current = issue.issue
    current_preflight = ResolveRearmPreflight(issue, linkage, external_holds) if isinstance(linkage, NoLinkedPr) else None
    reason = (
        StopReason.TARGET_TERMINAL if current.state != "open" or current.labels & {Label.AI_DONE, Label.AI_CLOSED}
        else StopReason.DECISION_CHANGED if isinstance(linkage, LinkedPrPresent) or (current.title, current.body, current.state) != (accepted.title, accepted.body, accepted.state)
        or current_preflight is not None and set(current_preflight.external_holds) != set(accepted_preflight.external_holds)
        else StopReason.REQUIRED_EFFECT_REJECTED if rejected_commands else None
    )
    if reason is not None:
        return supersede_resolve_rearm(snapshot, reason, evidence_id,
                                      verified_commands=verified_commands, uncertainty=uncertainty)
    if workspace is None or set(verified_commands) != required:
        return WorkflowUnchanged(snapshot)
    return complete_resolve_rearm(snapshot, issue, linkage, verified_commands=verified_commands,
                                  evidence_ids=(evidence_id,), now=now,
                                  external_holds=external_holds, workspace=workspace)
