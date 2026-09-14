"""Pure resolve-episode transitions under the ADR 0015 workflow authority."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime

from .actions import Relabel, Target
from .contracts import CommandId, CommandKind, WorkflowAttemptIdentity
from .eligibility import AllowedEligibility, LifecycleOperation
from .labels import Label
from .lifecycle import issue_eligibility
from .outcome import AgentResult, ResolveFollowup, budget_exhausted, infra_class_failure
from .workflow import (
    AuthorizedOperatorCommand, CancelRearm, CancelledRearm, CompletedRearm,
    ContinueGrantedRearm, EnsureLifecycleProjection, IssueObservation,
    LinkedPrObservation, LinkedPrPresent, NoAuthorizedRemediation, NoLinkedPr, ObservedIssue,
    ObservedTransition, OpenGate, OperatorActor, PendingRearm, RearmingGate,
    RearmNeedsDecision, RemoteOutcomeUncertain, RenewResolveAllowance,
    RequestRearm, ResolveAttemptResult, ResolveCeiling, ResolveEpisode,
    ResolveRearmPreflight, ResolveResultKind, SetGuidance, ClearGuidance, StopReason, SupersededRearm,
    UnresolvedGate, WorkflowChanged, WorkflowMutation, WorkflowRejected,
    WorkflowRejection, WorkflowSnapshot, WorkflowTransition, WorkflowUnchanged,
)
from .workflow_identity import (
    WorkflowFence, WorkflowKey, WorkflowRevision, validate_workflow_identifier,
)


def resolve_projection_command_id(key: WorkflowKey, request_id: str) -> CommandId:
    """One stable projection obligation per workflow and operator intent."""
    if not isinstance(key, WorkflowKey):
        raise ValueError("projection command requires a validated workflow key")
    validate_workflow_identifier(request_id, "request")
    material = f"{key.repository}\0{type(key.target).__name__}\0{key.target.number}\0{request_id}\0ensure_projection"
    return CommandId(hashlib.sha256(material.encode()).hexdigest())


def resolve_rearm_projection(
    snapshot: WorkflowSnapshot, *, expected_revision: str,
    external_holds: tuple[str, ...] = (),
) -> Relabel:
    """Only the selected gate's shared attention label may be released."""
    if not isinstance(snapshot.intent, PendingRearm):
        raise ValueError("resolve projection requires a pending re-arm")
    if not expected_revision or not isinstance(expected_revision, str):
        raise ValueError("resolve projection requires a fresh remote revision")
    remaining = any(_open(g).gate_id != snapshot.intent.gate_id for g in snapshot.gates)
    return Relabel(
        Target.ISSUE, snapshot.revision.fence.key.target.number,
        None if remaining or external_holds else Label.NEEDS_HUMAN_REVIEW,
        Label.NEEDS_HUMAN_REVIEW if remaining or external_holds else Label.AI_READY,
        expected_revision=expected_revision,
        workflow=snapshot.binding,
    )


def _identity(snapshot: WorkflowSnapshot, kind: str, detail: str) -> str:
    key = snapshot.revision.fence.key
    material = f"{key.repository}\0{key.target.number}\0{kind}\0{detail}"
    return f"{kind}:{hashlib.sha256(material.encode()).hexdigest()}"


def _reject(code: WorkflowRejection, reason: str) -> WorkflowRejected:
    return WorkflowRejected(code, reason)


def _open(gate: UnresolvedGate) -> OpenGate:
    return gate.gate if isinstance(gate, RearmingGate) else gate


def _next(snapshot: WorkflowSnapshot, *, advance_generation: bool = False) -> WorkflowRevision:
    current = snapshot.revision
    return WorkflowRevision(
        WorkflowFence(current.fence.key, current.fence.generation + int(advance_generation)),
        current.version + 1,
    )


def _ceiling_gates(snapshot: WorkflowSnapshot, episode: ResolveEpisode, ceiling: int) -> tuple[UnresolvedGate, ...]:
    if type(ceiling) is not int or not 0 <= ceiling <= 2**63 - 1:
        raise ValueError("resolve ceiling must be a nonnegative bounded integer")
    gates = snapshot.gates
    if ceiling and episode.failures >= ceiling and not any(
        isinstance(cause := _open(gate).cause, ResolveCeiling)
        and cause.episode_id == episode.episode_id
        for gate in gates
    ):
        gates += (OpenGate(
            _identity(snapshot, "resolve-ceiling", episode.episode_id),
            snapshot.revision.fence.generation,
            ResolveCeiling(episode.episode_id, episode.failures, ceiling, episode.last_failure),
            NoAuthorizedRemediation("renewing exhausted resolve allowance requires an operator"),
        ),)
    return gates


def reconcile_resolve_ceiling(
    snapshot: WorkflowSnapshot, *, ceiling: int, evidence_ids: tuple[str, ...],
) -> WorkflowTransition:
    """Imported exhausted episodes must be held before their first new attempt."""
    gates = _ceiling_gates(snapshot, snapshot.episode, ceiling)
    if gates == snapshot.gates:
        return WorkflowUnchanged(snapshot)
    after = replace(snapshot, revision=_next(snapshot), gates=gates)
    return WorkflowChanged(
        snapshot.revision, WorkflowMutation.OPEN_GATE, after,
        ObservedTransition(evidence_ids),
    )


def _observations(
    snapshot: WorkflowSnapshot, issue: IssueObservation, linkage: LinkedPrObservation,
    now: datetime,
) -> WorkflowRejected | None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("observation validation requires an aware current time")
    key = snapshot.revision.fence.key
    if issue.key != key or linkage.issue != key:
        return _reject(WorkflowRejection.INVALID_SCOPE, "observations belong to a different workflow")
    if not isinstance(issue, ObservedIssue):
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "a present issue observation is required")
    if any(not 0 <= (now - observed_at).total_seconds() <= 300 for observed_at in (issue.observed_at, linkage.observed_at)):
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "resolve re-arm observations are stale or from the future")
    if issue.issue.state != "open" or issue.issue.labels & {Label.AI_DONE, Label.AI_CLOSED}:
        return _reject(WorkflowRejection.INVALID_SCOPE, "resolve re-arm requires a nonterminal issue")
    if isinstance(linkage, LinkedPrPresent):
        return _reject(WorkflowRejection.INVALID_SCOPE, "an open linked PR already owns further work")
    if not isinstance(linkage, NoLinkedPr):
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "verified absence of an open linked PR is required")
    return None


def classify_resolve_attempt(
    result: AgentResult, followup: ResolveFollowup,
) -> ResolveAttemptResult | None:
    """Preserve the existing followup/classification policy and harness provenance.

    The caller supplies its actual followup, including the existing explicit
    requeue when a blocked result's requirements changed during the leg.
    Admission refusals lack an accounting-minted attempt and never buy a strike.
    """
    if not isinstance(result, AgentResult) or not isinstance(followup, ResolveFollowup):
        raise ValueError("resolve result and followup must be typed")
    if result.workflow_attempt is None:
        return None
    if followup is ResolveFollowup.PR_OPENED:
        kind = ResolveResultKind.PR_OPENED
    elif budget_exhausted(result.exit_reason):
        kind = ResolveResultKind.BUDGET_REFUSAL
    elif infra_class_failure(result):
        kind = ResolveResultKind.INFRA_FAILURE
    elif followup is ResolveFollowup.RETRY:
        kind = ResolveResultKind.COUNTED_REQUEUE
    else:
        kind = ResolveResultKind.BLOCKED
    # Match the retired Store's operator-facing last-error contract.
    summary = " ".join((result.summary or "").split())[:400]
    return ResolveAttemptResult(result.workflow_attempt, kind, summary)


def record_resolve_attempt(
    snapshot: WorkflowSnapshot,
    result: ResolveAttemptResult,
    *,
    ceiling: int,
    prior_result: ResolveAttemptResult | None = None,
) -> WorkflowTransition:
    """Deduplication evidence is read from SQLite by the committing adapter."""
    if type(ceiling) is not int or not 0 <= ceiling <= 2**63 - 1:
        raise ValueError("resolve ceiling must be a nonnegative bounded integer")
    attempt = result.attempt
    if attempt.binding.fence.key != snapshot.revision.fence.key:
        return _reject(WorkflowRejection.INVALID_SCOPE, "attempt belongs to a different workflow")
    if prior_result is not None:
        if prior_result.attempt != attempt or prior_result != result:
            return _reject(WorkflowRejection.INVALID_SCOPE, "conflicting result for the recorded attempt")
        return WorkflowUnchanged(snapshot)
    if attempt.binding != snapshot.binding:
        return WorkflowUnchanged(snapshot)
    if result.kind is not ResolveResultKind.COUNTED_REQUEUE:
        return WorkflowUnchanged(snapshot)
    if snapshot.episode.failures == 2**63 - 1:
        return _reject(WorkflowRejection.INVALID_SCOPE, "resolve failure counter is exhausted")
    episode = replace(
        snapshot.episode, failures=snapshot.episode.failures + 1,
        last_failure=result.summary or snapshot.episode.last_failure,
    )
    gates = _ceiling_gates(snapshot, episode, ceiling)
    after = replace(snapshot, revision=_next(snapshot), episode=episode, gates=gates)
    return WorkflowChanged(snapshot.revision, WorkflowMutation.RECORD_RESULT, after, result)


def request_resolve_rearm(
    snapshot: WorkflowSnapshot,
    command: RequestRearm,
    actor: OperatorActor,
    issue: IssueObservation,
    linkage: LinkedPrObservation,
    *,
    now: datetime,
    active_attempts: tuple[WorkflowAttemptIdentity, ...],
    prior_request: RequestRearm | None = None,
    external_holds: tuple[str, ...] = (),
) -> WorkflowTransition:
    if command.expected.fence.key != snapshot.revision.fence.key:
        return _reject(WorkflowRejection.INVALID_SCOPE, "request belongs to a different workflow")
    if prior_request is not None:
        if prior_request != command:
            return _reject(WorkflowRejection.INVALID_SCOPE, "request ID was reused with different content")
        return WorkflowUnchanged(snapshot)
    if command.expected != snapshot.revision:
        return _reject(WorkflowRejection.STALE_REVISION, "workflow changed before re-arm acceptance")
    if isinstance(snapshot.intent, PendingRearm):
        return _reject(WorkflowRejection.REARM_PENDING, "a re-arm already owns this workflow")
    if not isinstance(active_attempts, tuple) or any(not isinstance(a, WorkflowAttemptIdentity) for a in active_attempts):
        raise ValueError("active attempt evidence must be typed")
    if active_attempts:
        return _reject(WorkflowRejection.OTHER_HOLD, "active workflow attempts must drain before re-arm acceptance")
    failure = _observations(snapshot, issue, linkage, now)
    if failure is not None:
        return failure
    assert isinstance(issue, ObservedIssue) and isinstance(linkage, NoLinkedPr)
    return _request_rearm(snapshot, command, actor, ResolveRearmPreflight(issue, linkage, external_holds))


def _request_rearm(
    snapshot: WorkflowSnapshot, command: RequestRearm, actor: OperatorActor,
    preflight: ResolveRearmPreflight,
) -> WorkflowTransition:
    if command.expected != snapshot.revision:
        return _reject(WorkflowRejection.STALE_REVISION, "workflow changed before re-arm acceptance")
    if isinstance(snapshot.intent, PendingRearm):
        return _reject(WorkflowRejection.REARM_PENDING, "a re-arm already owns this workflow")
    gate = next((g for g in snapshot.gates if isinstance(g, OpenGate) and g.gate_id == command.gate_id), None)
    if gate is None or not isinstance(gate.cause, ResolveCeiling) or (
        gate.cause.episode_id != snapshot.episode.episode_id
        or snapshot.episode.failures < gate.cause.ceiling
    ):
        return _reject(WorkflowRejection.NOT_EXHAUSTED, "the selected current resolve episode is not exhausted")
    revision = _next(snapshot, advance_generation=True)
    guidance = snapshot.episode.guidance
    if isinstance(command.guidance, SetGuidance):
        guidance = command.guidance.text
    elif isinstance(command.guidance, ClearGuidance):
        guidance = ""
    episode = ResolveEpisode(
        _identity(snapshot, "resolve-episode", str(revision.fence.generation)),
        guidance=guidance,
    )
    pending = PendingRearm(
        command.request_id, gate.gate_id, snapshot.revision.fence.generation,
        revision.fence.generation,
        (RenewResolveAllowance(episode.episode_id), EnsureLifecycleProjection(revision.fence.key.target)),
        (resolve_projection_command_id(revision.fence.key, command.request_id),),
    )
    after = replace(
        snapshot, revision=revision, episode=episode, intent=pending,
        gates=tuple(RearmingGate(gate, command.request_id) if g == gate else g for g in snapshot.gates),
    )
    return WorkflowChanged(
        snapshot.revision, WorkflowMutation.REQUEST_REARM, after,
        AuthorizedOperatorCommand(actor, command, preflight),
    )


def _retire_rearm(
    snapshot: WorkflowSnapshot,
    reason: StopReason,
    evidence_id: str,
    *,
    verified_commands: tuple[CommandId, ...],
    uncertainty: tuple[RemoteOutcomeUncertain, ...],
    cancellation: tuple[CancelRearm, OperatorActor] | None = None,
) -> WorkflowTransition:
    pending = snapshot.intent
    if not isinstance(pending, PendingRearm):
        return _reject(WorkflowRejection.INVALID_SCOPE, "there is no pending re-arm to retire")
    required = set(pending.required_commands)
    if len(set(verified_commands)) != len(verified_commands) or not set(verified_commands) <= required:
        return _reject(WorkflowRejection.INVALID_SCOPE, "verified commands do not belong to this re-arm")
    if any(c.command_id not in required or c.command_id in verified_commands for c in uncertainty):
        return _reject(WorkflowRejection.INVALID_SCOPE, "uncertainty conflicts with required command evidence")
    retired = tuple(c for c in pending.required_commands if c not in verified_commands)
    if cancellation is not None:
        command, actor = cancellation
        intent = CancelledRearm(
            pending.request_id, pending.gate_id, pending.to_generation,
            command.cancellation_id, command.reason, verified_commands, retired,
        )
        audit = AuthorizedOperatorCommand(actor, command)
    else:
        intent = SupersededRearm(
            pending.request_id, pending.gate_id, pending.to_generation,
            reason, evidence_id, verified_commands, retired,
        )
        audit = ObservedTransition((evidence_id,))
    gates = tuple(g for g in snapshot.gates if _open(g).gate_id != pending.gate_id)
    gates += (OpenGate(
        _identity(snapshot, "rearm-stopped", pending.request_id),
        snapshot.revision.fence.generation,
        RearmNeedsDecision(pending.request_id, retired, reason),
        NoAuthorizedRemediation("the stopped re-arm requires a fresh operator decision"),
    ),)
    existing = {cause.command_id for g in gates if isinstance(cause := _open(g).cause, RemoteOutcomeUncertain)}
    for cause in uncertainty:
        if cause.command_id not in existing:
            gates += (OpenGate(
                _identity(snapshot, "command-uncertain", cause.command_id),
                snapshot.revision.fence.generation, cause,
                NoAuthorizedRemediation("only read-only outcome reconciliation remains authorized"),
            ),)
            existing.add(cause.command_id)
    after = replace(snapshot, revision=_next(snapshot), intent=intent, gates=gates)
    return WorkflowChanged(snapshot.revision, WorkflowMutation.STOP_REARM, after, audit)


def cancel_resolve_rearm(
    snapshot: WorkflowSnapshot, command: CancelRearm, actor: OperatorActor,
    *, verified_commands: tuple[CommandId, ...] = (),
    uncertainty: tuple[RemoteOutcomeUncertain, ...] = (),
) -> WorkflowTransition:
    if command.expected != snapshot.revision:
        return _reject(WorkflowRejection.STALE_REVISION, "workflow changed before cancellation")
    if not isinstance(snapshot.intent, PendingRearm) or snapshot.intent.request_id != command.request_id:
        return _reject(WorkflowRejection.INVALID_SCOPE, "cancellation names a different pending re-arm")
    return _retire_rearm(
        snapshot, StopReason.OPERATOR_CANCELLED, command.cancellation_id,
        verified_commands=verified_commands, uncertainty=uncertainty,
        cancellation=(command, actor),
    )


def supersede_resolve_rearm(
    snapshot: WorkflowSnapshot, reason: StopReason, evidence_id: str,
    *, verified_commands: tuple[CommandId, ...] = (),
    uncertainty: tuple[RemoteOutcomeUncertain, ...] = (),
) -> WorkflowTransition:
    if reason is StopReason.OPERATOR_CANCELLED:
        raise ValueError("operator cancellation requires an authenticated command")
    return _retire_rearm(
        snapshot, reason, evidence_id, verified_commands=verified_commands,
        uncertainty=uncertainty,
    )


def continue_resolve_rearm(
    snapshot: WorkflowSnapshot, command: ContinueGrantedRearm, actor: OperatorActor,
    issue: IssueObservation, linkage: LinkedPrObservation,
    *, now: datetime, active_attempts: tuple[WorkflowAttemptIdentity, ...],
    external_holds: tuple[str, ...] = (),
) -> WorkflowTransition:
    if command.expected != snapshot.revision:
        return _reject(WorkflowRejection.STALE_REVISION, "workflow changed before continuation")
    if isinstance(snapshot.intent, PendingRearm):
        return _reject(WorkflowRejection.REARM_PENDING, "a re-arm already owns this workflow")
    if not isinstance(snapshot.intent, (CancelledRearm, SupersededRearm)) or snapshot.intent.request_id != command.stopped_request_id:
        return _reject(WorkflowRejection.INVALID_SCOPE, "continuation requires the current stopped intent")
    if not isinstance(active_attempts, tuple) or any(not isinstance(a, WorkflowAttemptIdentity) for a in active_attempts):
        raise ValueError("active attempt evidence must be typed")
    if active_attempts:
        return _reject(WorkflowRejection.OTHER_HOLD, "active workflow attempts must drain before continuation")
    failure = _observations(snapshot, issue, linkage, now)
    if failure is not None:
        return failure
    assert isinstance(issue, ObservedIssue) and isinstance(linkage, NoLinkedPr)
    return _continue_rearm(snapshot, command, actor, ResolveRearmPreflight(issue, linkage, external_holds))


def _continue_rearm(
    snapshot: WorkflowSnapshot, command: ContinueGrantedRearm, actor: OperatorActor,
    preflight: ResolveRearmPreflight,
) -> WorkflowTransition:
    if command.expected != snapshot.revision:
        return _reject(WorkflowRejection.STALE_REVISION, "workflow changed before continuation")
    if not isinstance(snapshot.intent, (CancelledRearm, SupersededRearm)) or snapshot.intent.request_id != command.stopped_request_id:
        return _reject(WorkflowRejection.INVALID_SCOPE, "continuation requires the current stopped intent")
    gate = next((g for g in snapshot.gates if isinstance(g, OpenGate) and isinstance(g.cause, RearmNeedsDecision) and g.cause.stopped_request_id == command.stopped_request_id), None)
    if gate is None:
        return _reject(WorkflowRejection.INVALID_SCOPE, "stopped intent has no recorded decision gate")
    revision = _next(snapshot, advance_generation=True)
    pending = PendingRearm(
        command.request_id, gate.gate_id, snapshot.revision.fence.generation,
        revision.fence.generation, (EnsureLifecycleProjection(revision.fence.key.target),),
        (resolve_projection_command_id(revision.fence.key, command.request_id),),
    )
    after = replace(
        snapshot, revision=revision, intent=pending,
        gates=tuple(RearmingGate(gate, command.request_id) if g == gate else g for g in snapshot.gates),
    )
    return WorkflowChanged(
        snapshot.revision, WorkflowMutation.CONTINUE_REARM, after,
        AuthorizedOperatorCommand(actor, command, preflight),
    )


def complete_resolve_rearm(
    snapshot: WorkflowSnapshot, issue: IssueObservation, linkage: LinkedPrObservation,
    *, verified_commands: tuple[CommandId, ...], evidence_ids: tuple[str, ...],
    now: datetime,
    external_holds: tuple[str, ...] = (),
) -> WorkflowTransition:
    pending = snapshot.intent
    if not isinstance(pending, PendingRearm):
        return WorkflowUnchanged(snapshot)
    failure = _observations(snapshot, issue, linkage, now)
    if failure is not None:
        return failure
    if not pending.required_commands or set(verified_commands) != set(pending.required_commands) or len(set(verified_commands)) != len(verified_commands):
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "every required remote obligation must be verified")
    remaining = tuple(g for g in snapshot.gates if _open(g).gate_id != pending.gate_id)
    assert isinstance(issue, ObservedIssue)
    if remaining or external_holds:
        if Label.NEEDS_HUMAN_REVIEW not in issue.issue.labels:
            return _reject(WorkflowRejection.OTHER_HOLD, "the remote projection must preserve remaining holds")
    elif not isinstance(issue_eligibility(issue.issue, LifecycleOperation.RESOLVE), AllowedEligibility):
        return _reject(WorkflowRejection.OTHER_HOLD, "the verified remote state is not resolve-eligible")
    intent = CompletedRearm(pending.request_id, pending.gate_id, pending.to_generation, verified_commands)
    after = replace(snapshot, revision=_next(snapshot), intent=intent, gates=remaining)
    return WorkflowChanged(
        snapshot.revision, WorkflowMutation.RECONCILE_REARM, after,
        ObservedTransition(evidence_ids),
    )


def validate_resolve_transition(
    prior: WorkflowSnapshot, proposal: WorkflowChanged, *, ceiling: int,
    now: datetime,
    observations: tuple[ResolveRearmPreflight | ObservedIssue | LinkedPrPresent, ...] = (),
    accepted_preflight: ResolveRearmPreflight | None = None,
) -> None:
    """Recompute authoritative deltas; a well-formed snapshot is not permission.

    External observation freshness is established by the application under its
    acceptance barrier. The adapter additionally verifies referenced durable
    evidence, receipts and attempt identity within its transaction. This seam
    validates every state change without repeating policy in SQL.
    """
    if not isinstance(prior, WorkflowSnapshot) or not isinstance(proposal, WorkflowChanged):
        raise ValueError("workflow validation requires typed prior state and proposal")
    if prior.revision != proposal.expected or prior.authority != proposal.snapshot.authority:
        raise ValueError("workflow proposal changed its prior revision or family authority")
    audit = proposal.audit
    expected: WorkflowTransition
    if proposal.mutation is WorkflowMutation.RECORD_RESULT and isinstance(audit, ResolveAttemptResult):
        expected = record_resolve_attempt(prior, audit, ceiling=ceiling)
    elif proposal.mutation is WorkflowMutation.OPEN_GATE and isinstance(audit, ObservedTransition):
        expected = reconcile_resolve_ceiling(prior, ceiling=ceiling, evidence_ids=audit.evidence_ids)
    elif proposal.mutation is WorkflowMutation.REQUEST_REARM and isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.command, RequestRearm):
        preflight = audit.preflight
        assert preflight is not None
        expected = request_resolve_rearm(prior, audit.command, audit.actor, preflight.issue, preflight.linkage, now=now, active_attempts=(), external_holds=preflight.external_holds)
    elif proposal.mutation is WorkflowMutation.CONTINUE_REARM and isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.command, ContinueGrantedRearm):
        preflight = audit.preflight
        assert preflight is not None
        expected = continue_resolve_rearm(prior, audit.command, audit.actor, preflight.issue, preflight.linkage, now=now, active_attempts=(), external_holds=preflight.external_holds)
    elif proposal.mutation is WorkflowMutation.STOP_REARM:
        intent = proposal.snapshot.intent
        existing = {_open(g).gate_id for g in prior.gates}
        uncertainty = tuple(
            cause for gate in proposal.snapshot.gates
            if _open(gate).gate_id not in existing
            and isinstance(cause := _open(gate).cause, RemoteOutcomeUncertain)
        )
        if isinstance(intent, CancelledRearm) and isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.command, CancelRearm):
            expected = cancel_resolve_rearm(prior, audit.command, audit.actor, verified_commands=intent.verified_commands, uncertainty=uncertainty)
        elif isinstance(intent, SupersededRearm) and isinstance(audit, ObservedTransition):
            if len(observations) != 1:
                raise ValueError("supersession requires one exact durable observation")
            observation = observations[0]
            if not isinstance(observation, (ObservedIssue, LinkedPrPresent)):
                raise ValueError("absent or unavailable targets cannot justify supersession")
            observed_key = observation.key if isinstance(observation, ObservedIssue) else observation.issue
            if observed_key != prior.revision.fence.key or not 0 <= (now - observation.observed_at).total_seconds() <= 300:
                raise ValueError("supersession observation is foreign or stale")
            if intent.reason is StopReason.TARGET_TERMINAL:
                if not isinstance(observation, ObservedIssue) or (observation.issue.state == "open" and not observation.issue.labels & {Label.AI_DONE, Label.AI_CLOSED}):
                    raise ValueError("terminal supersession requires observed terminal issue state")
            elif intent.reason is StopReason.DECISION_CHANGED:
                if isinstance(observation, ObservedIssue):
                    if accepted_preflight is None or (
                        observation.issue.title, observation.issue.body, observation.issue.state,
                    ) == (
                        accepted_preflight.issue.issue.title, accepted_preflight.issue.issue.body,
                        accepted_preflight.issue.issue.state,
                    ):
                        raise ValueError("supersession requires materially changed issue decision facts")
            elif intent.reason is not StopReason.REQUIRED_EFFECT_REJECTED:
                raise ValueError("supersession reason is not authorized")
            expected = supersede_resolve_rearm(prior, intent.reason, intent.evidence_id, verified_commands=intent.verified_commands, uncertainty=uncertainty)
        else:
            raise ValueError("workflow stop does not carry its authorized disposition")
    elif proposal.mutation is WorkflowMutation.RECONCILE_REARM and isinstance(audit, ObservedTransition):
        pending, completed = prior.intent, proposal.snapshot.intent
        if not isinstance(pending, PendingRearm) or not isinstance(completed, CompletedRearm) or set(completed.verified_commands) != set(pending.required_commands):
            raise ValueError("workflow completion requires every pending obligation")
        if len(observations) != 1 or not isinstance(observations[0], ResolveRearmPreflight):
            raise ValueError("completion requires its exact durable remote projection observation")
        observation = observations[0]
        expected = complete_resolve_rearm(prior, observation.issue, observation.linkage,
            verified_commands=completed.verified_commands, evidence_ids=audit.evidence_ids,
            now=now, external_holds=observation.external_holds)
    else:
        raise ValueError("workflow mutation is not supported by resolve policy")
    if not isinstance(expected, WorkflowChanged):
        raise ValueError("proposal does not represent an authorized state transition")
    normalize = lambda s: replace(s, gates=tuple(sorted(s.gates, key=lambda g: _open(g).gate_id)))
    if normalize(expected.snapshot) != normalize(proposal.snapshot) or expected.audit != audit:
        raise ValueError("workflow proposal disagrees with the pure transition policy")
    if proposal.mutation in (WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM):
        intent = proposal.snapshot.intent
        assert isinstance(intent, PendingRearm)
        if {c.command_id for c in proposal.commands} != set(intent.required_commands):
            raise ValueError("accepted intent and durable outbox obligations must match exactly")
        assert isinstance(audit, AuthorizedOperatorCommand) and audit.preflight is not None
        for command in proposal.commands:
            if command.kind is not CommandKind.RELABEL or command.target != f"issue:{proposal.expected.fence.key.target.number}" or not command.expected_revision:
                raise ValueError("resolve projection requires a revision-fenced issue relabel")
            if command.expected_revision != audit.preflight.issue.issue.updated_at:
                raise ValueError("projection revision disagrees with its trusted observation")
            action = resolve_rearm_projection(proposal.snapshot, expected_revision=command.expected_revision, external_holds=audit.preflight.external_holds)
            expected_payload = {
                "target": Target.ISSUE.value, "number": action.number,
                "remove": action.remove.value if action.remove else None,
                "add": action.add.value if action.add else None,
                "expected_revision": action.expected_revision,
                "harness_signal": False, "removes": [], "completion_guard": None,
            }
            if dict(command.payload) != expected_payload:
                raise ValueError("projection command must preserve every unrelated hold and label")
    elif proposal.commands:
        raise ValueError("this resolve transition cannot enqueue remote commands")
