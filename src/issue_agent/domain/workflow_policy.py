"""Pure resolve-episode transitions under the ADR 0015 workflow authority."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta

from . import outcome
from .actions import Action, PostComment, Relabel, Target
from .contracts import CommandId, CommandKind, WorkflowAttemptIdentity
from .eligibility import AllowedEligibility, LifecycleOperation
from .labels import Label
from .lifecycle import completion_guard, hold_guard, issue_eligibility
from .outcome import AgentResult, ResolveFollowup, budget_exhausted, infra_class_failure
from .resolve_return import PendingResolveReturn, ResolveCleanupCompleted, ReturnedResolve
from .resolve_workspace import (
    EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared, PrepareResolveWorkspace,
    ResolveWorkspaceObserved,
)
from .workflow import (
    AuthorizeAbsentRetry, AuthorizedOperatorCommand, CancelRearm, CancelledRearm, CompletedRearm,
    ContinueGrantedRearm, EnsureLifecycleProjection, EvidenceUnavailable, FreshResolve, IssueObservation,
    LinkedPrObservation, LinkedPrPresent, NoAuthorizedRemediation, NoLinkedPr, ObservedIssue,
    ObservedTransition, OpenGate, OperatorActor, PendingRearm, RearmingGate,
    RearmNeedsDecision, RemoteOutcomeUncertain, RenewResolveAllowance, RetryAuthorizationRequired,
    RequestRearm, ResolveAdmission, ResolveAttemptResult, ResolveCeiling, ResolveEpisode,
    ResolveRearmAcceptance, ResolveRearmPreflight, ResolveResultKind, ResolveStage, SetGuidance, ClearGuidance, StopReason, SupersededRearm,
    UnresolvedGate, WorkflowChanged, WorkflowMutation, WorkflowRejected,
    WorkflowRejection, WorkflowSnapshot, WorkflowTransition, WorkflowUnchanged,
)
from .workflow_retry import RetryAuthorizationFacts, authorize_absent_retry
from .world import IssueView
from .workflow_identity import (
    FamilyAuthority, WorkflowBinding, WorkflowFence, WorkflowKey, WorkflowRevision, validate_workflow_identifier,
)


def return_gate_id(returned: ReturnedResolve) -> str:
    identity = returned.attempt
    material = f"{identity.binding.fence.key.repository}\0{identity.run_id}\0{identity.leg_id}"
    return "resolve-return:" + hashlib.sha256(material.encode()).hexdigest()


def return_observation_gate(pending: PendingResolveReturn) -> OpenGate:
    returned = pending.returned
    return OpenGate(
        return_gate_id(returned), returned.attempt.binding.fence.generation,
        EvidenceUnavailable(str(returned.attempt.run_id), returned.returned_at,
                            pending.failed_reads, pending.last_error or "paid return awaits complete issue and linkage observations"),
        NoAuthorizedRemediation("read-only completion observation is required; do not repeat the paid leg"),
    )


def update_return_observation(
    snapshot: WorkflowSnapshot, returned: ReturnedResolve, pending: PendingResolveReturn | None,
) -> WorkflowSnapshot:
    if snapshot.revision.fence.key != returned.attempt.binding.fence.key:
        raise ValueError("returned observation belongs to another workflow")
    if pending is not None and (pending.returned != returned or snapshot.binding != returned.attempt.binding):
        raise ValueError("cannot open a return gate for a stale or different attempt")
    gates = tuple(g for g in snapshot.gates if _open(g).gate_id != return_gate_id(returned))
    if pending is not None:
        gates += (return_observation_gate(pending),)
    if gates == snapshot.gates:
        return snapshot
    return replace(snapshot, revision=_next(snapshot), gates=gates)


def resolve_return_actions(snapshot: WorkflowSnapshot, actions: tuple[Action, ...], *, prior: WorkflowSnapshot) -> tuple[Action, ...]:
    """Keep projection debt under existing holds; a new ceiling owns escalation."""
    existing = {_open(g).gate_id for g in prior.gates}
    newly_exhausted = any(_open(g).gate_id not in existing and isinstance(_open(g).cause, ResolveCeiling)
                          for g in snapshot.gates)
    return tuple(action for action in actions if not (
        newly_exhausted and isinstance(action, Relabel) and action.add is Label.AI_READY
    ))


def apply_return_cleanup(snapshot: WorkflowSnapshot, cleanup: ResolveCleanupCompleted) -> WorkflowSnapshot:
    """Removed worktree content cannot retain an authored checkpoint or handle."""
    if not isinstance(cleanup, ResolveCleanupCompleted) or cleanup.attempt.binding != snapshot.binding:
        raise ValueError("terminal cleanup must belong to the current episode")
    fresh = FreshResolve(last_applied_admission_version=snapshot.episode.continuation.last_applied_admission_version)
    return replace(snapshot, episode=replace(snapshot.episode, continuation=fresh))


def defer_return_observation(pending: PendingResolveReturn, error: str, *, now: datetime) -> PendingResolveReturn:
    # Observation continues at a bounded cadence even after prolonged failure;
    # neither elapsed time nor a failed read authorizes a new execution.
    if not isinstance(error, str) or not error.strip() or len(error) > 16384 or "\x00" in error:
        raise ValueError("return observation failure requires bounded error text")
    if now.tzinfo is None or now < pending.returned.returned_at:
        raise ValueError("return observation clock precedes the paid return")
    failures = pending.failed_reads + 1
    delay = min(3600, 5 * 2 ** min(failures - 1, 10))
    return replace(pending, failed_reads=failures, last_error=error,
                   next_read_at=max(pending.next_read_at, now + timedelta(seconds=delay)))


def decide_return(
    returned: ReturnedResolve, issue: ObservedIssue, linkage: NoLinkedPr | LinkedPrPresent,
    *, now: datetime,
) -> tuple[ResolveAttemptResult, tuple[Action, ...]]:
    """Immediate and recovered completion share stage-aware classification."""
    if not isinstance(returned, ReturnedResolve) or not isinstance(issue, ObservedIssue) or not isinstance(linkage, (NoLinkedPr, LinkedPrPresent)):
        raise ValueError("completion requires a typed return and complete observations")
    key = returned.attempt.binding.fence.key
    if issue.key != key or linkage.issue != key:
        raise ValueError("return observations belong to another workflow")
    if not isinstance(now, datetime) or now.tzinfo is None or any(
        not 0 <= (now - at).total_seconds() <= 300 or at < returned.returned_at
        for at in (issue.observed_at, linkage.observed_at)
    ):
        raise ValueError("return observations must be fresh and postdate the paid return")
    result = returned.result()
    number = key.target.number
    pr = linkage.pr if isinstance(linkage, LinkedPrPresent) else None
    changed = pr is None and result.status == "blocked" and not (
        infra_class_failure(result) or budget_exhausted(result.exit_reason)
    ) and (issue.issue.title, issue.issue.body, issue.issue.author) != (
        returned.inputs.title, returned.inputs.body, returned.inputs.author,
    )
    closed_success = pr is None and issue.issue.state == "closed" and not result.is_error and result.status in ("pushed", "done")
    followup = (ResolveFollowup.PR_OPENED if pr is not None else ResolveFollowup.RETRY if changed
                else ResolveFollowup.BLOCKED if returned.stage is ResolveStage.RESUME
                else outcome.resolve_followup(result, False))
    classified = classify_resolve_attempt(result, followup)
    assert classified is not None
    kind = classified.kind
    if budget_exhausted(result.exit_reason):
        kind = ResolveResultKind.BUDGET_REFUSAL
    elif infra_class_failure(result):
        kind = ResolveResultKind.INFRA_FAILURE
    elif closed_success or (returned.stage is ResolveStage.RESUME and pr is None and not result.is_error and result.status != "blocked"):
        kind = ResolveResultKind.STAGE_PROGRESSED
    elif issue.issue.state == "closed" and pr is None:
        kind = ResolveResultKind.BLOCKED
    classified = replace(classified, kind=kind, stage=returned.stage,
                         session=None if kind in (ResolveResultKind.INFRA_FAILURE, ResolveResultKind.BUDGET_REFUSAL) else returned.session)
    if closed_success:
        return replace(classified, session=None), tuple(outcome.resolve_actions(number, result, None, issue_closed_without_pr=True))
    if not isinstance(issue_eligibility(issue.issue, LifecycleOperation.COMPLETE_RESOLVE), AllowedEligibility):
        return classified, ()
    if changed:
        actions: list[Action] = [Relabel(Target.ISSUE, number, Label.AI_WORKING, Label.AI_READY)]
    elif returned.stage is ResolveStage.RESUME and pr is None and not budget_exhausted(result.exit_reason):
        actions = outcome.resume_actions(number, result, None)
    else:
        actions = outcome.resolve_actions(number, result, pr)
    removes: list[Label] = []
    adds: list[Label] = []
    comments: list[str] = []
    others: list[Action] = []
    for action in actions:
        if isinstance(action, Relabel) and action.target is Target.ISSUE:
            removes.extend(action.removed_labels)
            adds.extend(action.added_labels)
        elif isinstance(action, PostComment) and action.target is Target.ISSUE:
            comments.append(action.body)
        elif isinstance(action, Relabel) and action.target is Target.PR:
            assert isinstance(linkage, LinkedPrPresent)
            others.append(replace(action, expected_revision=linkage.head_sha, workflow=returned.attempt.binding))
        else:
            raise ValueError("ordinary resolve completion emitted an unsupported effect")
    removes = list(dict.fromkeys(removes))
    adds = list(dict.fromkeys(adds))
    compact: tuple[Action, ...] = ()
    if removes or adds or comments:
        compact = (Relabel(
            Target.ISSUE, number, removes[0] if removes else None, adds[0] if adds else None,
            expected_revision=issue.issue.updated_at, removes=tuple(removes[1:]), adds=tuple(adds[1:]),
            comment="\n\n".join(comments)[:65536],
            completion_guard=completion_guard(issue.issue, LifecycleOperation.COMPLETE_RESOLVE),
            workflow=returned.attempt.binding,
        ),)
    return classified, compact + tuple(others)


def initial_resolve_workflow(
    observation: ObservedIssue, authority: FamilyAuthority, *, now: datetime,
) -> WorkflowSnapshot:
    """Initial state for proven new targets, never a substitute for a read.

    The adapter proves absence under an activated epoch in the same transaction
    that installs this value. Closed/held observations remain subject to normal
    lifecycle eligibility; creating a record does not grant dispatch permission.
    """
    if not isinstance(observation, ObservedIssue) or not isinstance(authority, FamilyAuthority):
        raise ValueError("workflow discovery requires a present typed issue and family authority")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("workflow discovery requires an aware current time")
    if not 0 <= (now - observation.observed_at).total_seconds() <= 300:
        raise ValueError("workflow discovery observation is stale or from the future")
    key = observation.key
    material = f"{key.repository}\0{key.target.number}\0{authority.family.value}\0{authority.epoch}\0initial"
    episode_id = "resolve-initial:" + hashlib.sha256(material.encode()).hexdigest()
    return WorkflowSnapshot(
        WorkflowRevision(WorkflowFence(key, 0), 0), authority, ResolveEpisode(episode_id),
    )


def resolve_projection_command_id(key: WorkflowKey, request_id: str) -> CommandId:
    """One stable projection obligation per workflow and operator intent."""
    if not isinstance(key, WorkflowKey):
        raise ValueError("projection command requires a validated workflow key")
    validate_workflow_identifier(request_id, "request")
    material = f"{key.repository}\0{type(key.target).__name__}\0{key.target.number}\0{request_id}\0ensure_projection"
    return CommandId(hashlib.sha256(material.encode()).hexdigest())


def resolve_rearm_projection(
    snapshot: WorkflowSnapshot, issue: IssueView, *,
    external_holds: tuple[str, ...] = (),
) -> Relabel:
    """Only the selected gate's shared attention label may be released.

    The command is fenced by the accepted hold facts (ADR 0015), not by the
    raw issue revision: harmless activity cannot supersede it, and its own
    half-applied relabel still satisfies the guard on retry.
    """
    if not isinstance(snapshot.intent, PendingRearm):
        raise ValueError("resolve projection requires a pending re-arm")
    if not isinstance(issue, IssueView) or issue.number != snapshot.revision.fence.key.target.number:
        raise ValueError("resolve projection requires its accepted issue observation")
    if not issue.updated_at or not isinstance(issue.updated_at, str):
        raise ValueError("resolve projection requires a fresh remote revision")
    remaining = any(
        _open(g).gate_id != snapshot.intent.gate_id and not _retired_uncertainty(snapshot, g)
        for g in snapshot.gates
    )
    return Relabel(
        Target.ISSUE, snapshot.revision.fence.key.target.number,
        None if remaining or external_holds else Label.NEEDS_HUMAN_REVIEW,
        Label.NEEDS_HUMAN_REVIEW if remaining or external_holds else Label.AI_READY,
        expected_revision=issue.updated_at,
        workflow=snapshot.binding,
        hold_guard=hold_guard(issue),
    )


def _retired_uncertainty(snapshot: WorkflowSnapshot, gate: UnresolvedGate) -> bool:
    """An uncertainty gate over an obligation this continuation replaces.

    It is not an independent hold: the replacement projects the same outcome
    and stays held until reconciliation resolves the prior effect (ADR 0015),
    so planning a hold-preserving write around it would conflict with the
    prior write if that write landed.
    """
    pending = snapshot.intent
    assert isinstance(pending, PendingRearm)
    selected = next((_open(g) for g in snapshot.gates if _open(g).gate_id == pending.gate_id), None)
    if selected is None or not isinstance(selected.cause, RearmNeedsDecision):
        return False
    cause = _open(gate).cause
    return (
        isinstance(cause, (RemoteOutcomeUncertain, RetryAuthorizationRequired))
        and cause.command_id in selected.cause.obligations
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
    admission: ResolveAdmission,
    prior_result: ResolveAttemptResult | None = None,
) -> WorkflowTransition:
    """Deduplication evidence is read from SQLite by the committing adapter."""
    if type(ceiling) is not int or not 0 <= ceiling <= 2**63 - 1:
        raise ValueError("resolve ceiling must be a nonnegative bounded integer")
    attempt = result.attempt
    if not isinstance(admission, ResolveAdmission) or admission.attempt != attempt or admission.stage is not result.stage:
        return _reject(WorkflowRejection.INVALID_SCOPE, "result disagrees with its captured admission")
    if admission.revision.version > snapshot.revision.version or admission.revision.fence.generation > snapshot.revision.fence.generation:
        return _reject(WorkflowRejection.INVALID_SCOPE, "result admission is newer than the workflow")
    if attempt.binding.fence.key != snapshot.revision.fence.key:
        return _reject(WorkflowRejection.INVALID_SCOPE, "attempt belongs to a different workflow")
    if prior_result is not None:
        if prior_result.attempt != attempt or prior_result != result:
            return _reject(WorkflowRejection.INVALID_SCOPE, "conflicting result for the recorded attempt")
        return WorkflowUnchanged(snapshot)
    if attempt.binding != snapshot.binding:
        return WorkflowUnchanged(snapshot)
    # Router selection is audited but does not own a resolve continuation or a
    # resolve strike. Infrastructure/admission failures cannot replace a usable
    # checkpoint with an unproven provider handle.
    if result.stage is ResolveStage.ROUTE or result.kind in (ResolveResultKind.INFRA_FAILURE, ResolveResultKind.BUDGET_REFUSAL):
        return WorkflowUnchanged(snapshot)
    continuation = snapshot.episode.continuation
    if admission.revision.version > continuation.last_applied_admission_version:
        session = result.session
        if result.stage is ResolveStage.RESUME and result.kind is ResolveResultKind.BLOCKED:
            session = None
        continuation = replace(continuation, session=session, last_applied_admission_version=admission.revision.version)
    episode = replace(snapshot.episode, continuation=continuation)
    if result.kind is ResolveResultKind.COUNTED_REQUEUE:
        if episode.failures == 2**63 - 1:
            return _reject(WorkflowRejection.INVALID_SCOPE, "resolve failure counter is exhausted")
        episode = replace(episode, failures=episode.failures + 1, last_failure=result.summary or episode.last_failure)
    if episode == snapshot.episode:
        return WorkflowUnchanged(snapshot)
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
    workspace: ResolveWorkspaceObserved | None = None,
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
    if not isinstance(workspace, ResolveWorkspaceObserved):
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "resolve re-arm requires verified workspace observations")
    if workspace.source != snapshot.binding:
        return _reject(WorkflowRejection.INVALID_SCOPE, "workspace observation belongs to a different source binding")
    if not 0 <= (now - workspace.base.observed_at).total_seconds() <= 300:
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "workspace base observation is stale or from the future")
    return _request_rearm(snapshot, command, actor, ResolveRearmAcceptance(
        ResolveRearmPreflight(issue, linkage, external_holds), workspace,
    ))


def _request_rearm(
    snapshot: WorkflowSnapshot, command: RequestRearm, actor: OperatorActor,
    preflight: ResolveRearmAcceptance,
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
    observed = preflight.workspace
    handoff = EpisodeWorkspaceHandoff(
        command.request_id, snapshot.binding,
        WorkflowBinding(revision.fence, snapshot.authority, episode.episode_id),
        observed.predecessor, observed.branch, observed.base,
    )
    pending = PendingRearm(
        command.request_id, gate.gate_id, snapshot.revision.fence.generation,
        revision.fence.generation,
        (RenewResolveAllowance(episode.episode_id), PrepareResolveWorkspace(handoff), EnsureLifecycleProjection(revision.fence.key.target)),
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
            pending.handoff,
        )
        audit = AuthorizedOperatorCommand(actor, command)
    else:
        intent = SupersededRearm(
            pending.request_id, pending.gate_id, pending.to_generation,
            reason, evidence_id, verified_commands, retired,
            pending.handoff,
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
        revision.fence.generation, (PrepareResolveWorkspace(snapshot.intent.handoff), EnsureLifecycleProjection(revision.fence.key.target)),
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
    workspace: EpisodeWorkspacePrepared | None = None,
) -> WorkflowTransition:
    pending = snapshot.intent
    if not isinstance(pending, PendingRearm):
        return WorkflowUnchanged(snapshot)
    failure = _observations(snapshot, issue, linkage, now)
    if failure is not None:
        return failure
    if not isinstance(workspace, EpisodeWorkspacePrepared):
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "workspace preparation must be verified before re-arm completion")
    if workspace.handoff != pending.handoff or workspace.performed_under.fence.generation > snapshot.revision.fence.generation:
        return _reject(WorkflowRejection.INVALID_SCOPE, "workspace acknowledgement does not belong to this granted obligation")
    if workspace.observed_at > now:
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "workspace acknowledgement is from the future")
    if not pending.required_commands or set(verified_commands) != set(pending.required_commands) or len(set(verified_commands)) != len(verified_commands):
        return _reject(WorkflowRejection.OBSERVATION_UNAVAILABLE, "every required remote obligation must be verified")
    remaining = tuple(g for g in snapshot.gates if _open(g).gate_id != pending.gate_id)
    assert isinstance(issue, ObservedIssue)
    if remaining or external_holds:
        if Label.NEEDS_HUMAN_REVIEW not in issue.issue.labels:
            return _reject(WorkflowRejection.OTHER_HOLD, "the remote projection must preserve remaining holds")
    elif not isinstance(issue_eligibility(issue.issue, LifecycleOperation.RESOLVE), AllowedEligibility):
        return _reject(WorkflowRejection.OTHER_HOLD, "the verified remote state is not resolve-eligible")
    intent = CompletedRearm(pending.request_id, pending.gate_id, pending.to_generation, verified_commands, workspace)
    after = replace(snapshot, revision=_next(snapshot), intent=intent, gates=remaining)
    return WorkflowChanged(
        snapshot.revision, WorkflowMutation.RECONCILE_REARM, after,
        ObservedTransition(evidence_ids),
    )


def validate_resolve_transition(
    prior: WorkflowSnapshot, proposal: WorkflowChanged, *, ceiling: int,
    now: datetime,
    observations: tuple[ResolveRearmPreflight | ObservedIssue | LinkedPrPresent, ...] = (),
    accepted_preflight: ResolveRearmPreflight | ResolveRearmAcceptance | None = None,
    admission: ResolveAdmission | None = None,
    retry_facts: RetryAuthorizationFacts | None = None,
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
        if admission is None:
            raise ValueError("result transition requires its captured admission")
        expected = record_resolve_attempt(prior, audit, ceiling=ceiling, admission=admission)
    elif proposal.mutation is WorkflowMutation.OPEN_GATE and isinstance(audit, ObservedTransition):
        expected = reconcile_resolve_ceiling(prior, ceiling=ceiling, evidence_ids=audit.evidence_ids)
    elif proposal.mutation is WorkflowMutation.REQUEST_REARM and isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.command, RequestRearm):
        preflight = audit.preflight
        assert isinstance(preflight, ResolveRearmAcceptance)
        expected = request_resolve_rearm(prior, audit.command, audit.actor, preflight.issue, preflight.linkage, now=now, active_attempts=(), external_holds=preflight.external_holds, workspace=preflight.workspace)
    elif proposal.mutation is WorkflowMutation.CONTINUE_REARM and isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.command, ContinueGrantedRearm):
        preflight = audit.preflight
        if not isinstance(preflight, (ResolveRearmPreflight, ResolveRearmAcceptance)):
            raise ValueError("resolve continuation requires its resolve preflight")
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
            if not isinstance(observation, (ResolveRearmPreflight, ObservedIssue, LinkedPrPresent)):
                raise ValueError("absent or unavailable targets cannot justify supersession")
            observed_target = observation.issue if isinstance(observation, ResolveRearmPreflight) else observation
            observed_key = observed_target.key if isinstance(observed_target, ObservedIssue) else observed_target.issue
            if observed_key != prior.revision.fence.key or not 0 <= (now - observed_target.observed_at).total_seconds() <= 300:
                raise ValueError("supersession observation is foreign or stale")
            if intent.reason is StopReason.TARGET_TERMINAL:
                if not isinstance(observation, ObservedIssue) or (observation.issue.state == "open" and not observation.issue.labels & {Label.AI_DONE, Label.AI_CLOSED}):
                    raise ValueError("terminal supersession requires observed terminal issue state")
            elif intent.reason is StopReason.DECISION_CHANGED:
                if isinstance(observation, ResolveRearmPreflight):
                    if not 0 <= (now - observation.linkage.observed_at).total_seconds() <= 300:
                        raise ValueError("supersession linkage is stale")
                    if accepted_preflight is None or set(observation.external_holds) == set(accepted_preflight.external_holds):
                        raise ValueError("supersession requires changed hold facts")
                elif isinstance(observation, ObservedIssue):
                    # Title/body/comment edits are not decision facts (ADR 0015).
                    raise ValueError("decision supersession requires an observed linked PR or changed holds")
            elif intent.reason is not StopReason.REQUIRED_EFFECT_REJECTED:
                raise ValueError("supersession reason is not authorized")
            expected = supersede_resolve_rearm(prior, intent.reason, intent.evidence_id, verified_commands=intent.verified_commands, uncertainty=uncertainty)
        else:
            raise ValueError("workflow stop does not carry its authorized disposition")
    elif proposal.mutation is WorkflowMutation.AUTHORIZE_RETRY and isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.command, AuthorizeAbsentRetry):
        if not isinstance(retry_facts, RetryAuthorizationFacts):
            raise ValueError("retry authorization requires its harness-owned command facts")
        expected = authorize_absent_retry(prior, audit.command, audit.actor, retry_facts)
    elif proposal.mutation is WorkflowMutation.RECONCILE_REARM and isinstance(audit, ObservedTransition):
        pending, completed = prior.intent, proposal.snapshot.intent
        if not isinstance(pending, PendingRearm) or not isinstance(completed, CompletedRearm) or set(completed.verified_commands) != set(pending.required_commands):
            raise ValueError("workflow completion requires every pending obligation")
        if len(observations) != 1 or not isinstance(observations[0], ResolveRearmPreflight):
            raise ValueError("completion requires its exact durable remote projection observation")
        observation = observations[0]
        expected = complete_resolve_rearm(prior, observation.issue, observation.linkage,
            verified_commands=completed.verified_commands, evidence_ids=audit.evidence_ids,
            now=now, external_holds=observation.external_holds, workspace=completed.workspace)
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
        assert isinstance(audit, AuthorizedOperatorCommand)
        if not isinstance(audit.preflight, (ResolveRearmPreflight, ResolveRearmAcceptance)):
            raise ValueError("resolve projection requires its resolve preflight")
        for command in proposal.commands:
            if command.kind is not CommandKind.RELABEL or command.target != f"issue:{proposal.expected.fence.key.target.number}" or not command.expected_revision:
                raise ValueError("resolve projection requires a revision-fenced issue relabel")
            if command.expected_revision != audit.preflight.issue.issue.updated_at:
                raise ValueError("projection revision disagrees with its trusted observation")
            action = resolve_rearm_projection(proposal.snapshot, audit.preflight.issue.issue, external_holds=audit.preflight.external_holds)
            assert action.hold_guard is not None
            expected_payload = {
                "target": Target.ISSUE.value, "number": action.number,
                "remove": action.remove.value if action.remove else None,
                "add": action.add.value if action.add else None,
                "expected_revision": action.expected_revision,
                "harness_signal": False, "removes": [], "completion_guard": None,
                "hold_guard": {"state_reason": action.hold_guard.state_reason,
                               "labels": sorted(label.value for label in action.hold_guard.labels)},
            }
            if dict(command.payload) != expected_payload:
                raise ValueError("projection command must preserve every unrelated hold and label")
    elif proposal.commands:
        raise ValueError("this resolve transition cannot enqueue remote commands")
