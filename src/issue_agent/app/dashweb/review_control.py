"""PR-scoped review-rejection operator projection and commands; no legacy unblock fallback.

Commands are accepted by the review owner's `SQLiteReviewWorkflowControl`
under its exclusive deployment barriers; nothing here executes an effect.
"""

import os

from ...adapters.review_workflow_control import SQLiteReviewWorkflowControl
from ...domain import workflow as w
from ...domain.review_control import (
    ActionOffered, DismissalObligation, ProjectionObligation, evidence_hold, rearm_obligations, refused_projection,
    review_gate_actions, execution_recovery_choices,
)
from ...domain.review_dispatch_record import (
    RecordedDispatch, RecordedReviewDecision, ReviewDeferred, ReviewInFlight, ReviewNotAdmitted, ReviewTerminal,
    ReviewWithheld,
)
from ...domain.review_rearm import (
    AbsentReviewControl, MigrationRequiredReviewControl, ReviewControlView,
    UnreadableHoldRestoration,
)
from ...domain.review_rejection import MAX_COUNTED_REVIEWS, ReviewProjectionRefused, ReviewRejectionCeiling
from ...domain.review_execution import CurrentReviewExecution
from ...domain.review_workflow import (
    CancelledReviewRearm, CompletedReviewRearm, FreshReviewRequired, PendingReviewRearm,
    ReviewRearmIntent, SupersededReviewRearm,
)
from ...domain.review_workflow_policy import (
    MAX_HOLD_RESTORES, ReleaseProjectionPending, ReviewHeld, ReviewKind, ReviewNotOwed, ReviewOwed,
    SubmissionUnresolved,
)
from ...domain.workflow_identity import PullRequestRef, WorkflowKey
from . import review_contracts as wire
from .resolve_control import control_error, recovery_control
from .resolve_contracts import RecoveryUnavailable


def _rearm(intent: ReviewRearmIntent | None, view: ReviewControlView) -> wire.ReviewRearm | None:
    if intent is None:
        return None
    if isinstance(intent, PendingReviewRearm):
        progress = {item.command_id: item.status.value for item in view.command_progress}
        current = view.snapshot.intent
        commands = []
        for command_id, obligation in (rearm_obligations(view.snapshot, intent) if current == intent else
                                       tuple((c, None) for c in intent.required_commands)):
            label = (wire.ReviewDismissalObligation(kind="dismiss_review", review_id=obligation.review.review_id)
                     if isinstance(obligation, DismissalObligation) else
                     wire.ReviewProjectionObligation(
                         kind="projection", add=obligation.plan.add.value,
                         remove=obligation.plan.remove.value if obligation.plan.remove is not None else None)
                     if isinstance(obligation, ProjectionObligation) else
                     wire.ReviewUnlabelledObligation(kind="unlabelled"))
            commands.append(wire.ReviewRearmCommand(command_id=command_id, obligation=label,
                                                    status=progress.get(command_id, "pending")))
        return wire.ReviewPendingRearm(
            state="pending", request_id=intent.request_id, gate_id=intent.gate_id, generation=intent.to_generation,
            authorized_reviews=[review.review_id for review in intent.authorized], commands=commands,
        )
    if isinstance(intent, CompletedReviewRearm):
        return wire.ReviewCompletedRearm(state="completed", request_id=intent.request_id, gate_id=intent.gate_id,
                                         generation=intent.to_generation,
                                         verified_commands=list(intent.verified_commands))
    assert isinstance(intent, (CancelledReviewRearm, SupersededReviewRearm))
    return wire.ReviewStoppedRearm(
        state="cancelled" if isinstance(intent, CancelledReviewRearm) else "superseded",
        request_id=intent.request_id, gate_id=intent.gate_id, generation=intent.to_generation,
        reason=intent.reason if isinstance(intent, CancelledReviewRearm) else intent.reason.value,
        authorized_reviews=[review.review_id for review in intent.authorized],
        verified_commands=list(intent.verified_commands), retired_commands=list(intent.retired_obligations),
    )


def _hold(gate: w.OpenGate, view: ReviewControlView) -> wire.ReviewHoldState:
    restoration = next((item for item in view.hold_restorations if item.gate_id == gate.gate_id), None)
    if isinstance(restoration, UnreadableHoldRestoration):
        return wire.ReviewHoldUnreadable(state="unreadable", reason=restoration.reason)
    restores = restoration.restores if restoration is not None else 0
    if restoration is not None and restoration.contested:
        return wire.ReviewHoldContested(state="contested", restores=restores, restore_limit=MAX_HOLD_RESTORES)
    return wire.ReviewHoldUncontested(state="restoring", restores=restores, restore_limit=MAX_HOLD_RESTORES)


def _cause_kind(cause: w.GateCause) -> wire.ReviewCauseKind:
    kinds: tuple[tuple[type, wire.ReviewCauseKind], ...] = (
        (w.ReviewExecutionCeiling, "review_execution_ceiling"), (w.ReviewVerdictHold, "review_verdict_hold"),
        (ReviewRejectionCeiling, "review_rejection_ceiling"), (w.RemoteOutcomeUncertain, "remote_uncertain"),
        (w.RetryAuthorizationRequired, "retry_authorization_required"), (w.EvidenceUnavailable, "evidence_unavailable"),
        (w.RearmNeedsDecision, "rearm_needs_decision"), (ReviewProjectionRefused, "review_projection_refused"),
    )
    for kind, name in kinds:
        if isinstance(cause, kind):
            return name
    raise ValueError(f"review control has no wire form for {type(cause).__name__}")


def _cause(cause: w.GateCause, gate: w.OpenGate, view: ReviewControlView) -> wire.ReviewGateCause:
    facts = next((item for item in view.gate_facts_unavailable if item.gate_id == gate.gate_id), None)
    if facts is not None:
        return wire.ReviewGateFactsUnavailableCause(kind="facts_unavailable", underlying=_cause_kind(cause),
                                                    reason=facts.reason)
    if isinstance(cause, w.ReviewExecutionCeiling):
        return wire.ReviewExecutionCeilingCause(
            kind="review_execution_ceiling", execution_epoch=cause.execution_epoch,
            execution_episode=cause.execution_episode, head_sha=cause.head_sha,
            failures=cause.failures, ceiling=cause.ceiling,
        )
    if isinstance(cause, w.ReviewVerdictHold):
        return wire.ReviewVerdictHoldCause(kind="review_verdict_hold",
            submission_command_id=cause.submission_command_id, head_sha=cause.head_sha)
    if isinstance(cause, ReviewRejectionCeiling):
        hold = _hold(gate, view)
        return wire.ReviewRejectionCeilingCause(
            kind="review_rejection_ceiling", episode_id=cause.episode_id, head_sha=cause.head_sha,
            review_bot=cause.review_bot, ceiling=cause.ceiling, counted_total=len(cause.counted),
            counted=[wire.ReviewCountedReview(review_id=review.review_id, author=cause.review_bot,
                                              commit_id=review.commit_id) for review in cause.counted],
            authorization_bound=MAX_COUNTED_REVIEWS, non_resetting_commits=list(cause.non_resetting_commits),
            hold=hold,
        )
    if isinstance(cause, w.RemoteOutcomeUncertain):
        return wire.ReviewRemoteUncertainCause(
            kind="remote_uncertain", command_id=cause.command_id, unresolved_since=cause.unresolved_since.isoformat(),
            failed_reads=cause.failed_reads, last_error=cause.last_error)
    if isinstance(cause, w.RetryAuthorizationRequired):
        return wire.ReviewRetryAuthorizationRequiredCause(
            kind="retry_authorization_required", command_id=cause.command_id,
            absence_evidence_id=cause.absence_evidence_id, retry_authorizable=gate.gate_id in view.retry_authorizable)
    if isinstance(cause, w.EvidenceUnavailable):
        kind = evidence_hold(gate)
        assert kind is not None
        return wire.ReviewEvidenceUnavailableCause(
            kind="evidence_unavailable", evidence=kind.value, source=cause.required_fact,
            unresolved_since=cause.unresolved_since.isoformat(), failed_reads=cause.failed_reads,
            last_error=cause.last_error)
    if isinstance(cause, w.RearmNeedsDecision):
        return wire.ReviewRearmNeedsDecisionCause(
            kind="rearm_needs_decision", stopped_request_id=cause.stopped_request_id,
            obligations=list(cause.obligations), reason=cause.reason.value)
    if isinstance(cause, ReviewProjectionRefused):
        return wire.ReviewProjectionRefusedCause(
            kind="review_projection_refused", projection=refused_projection(view.snapshot, cause).value,
            command_id=cause.command_id, generation=cause.generation, reason=cause.reason)
    # A review snapshot refuses resolve causes at construction; this is unreachable
    # unless a new cause variant lands without its operator projection.
    raise ValueError(f"review control has no wire form for {type(cause).__name__}")


def _gate(gate: w.UnresolvedGate, view: ReviewControlView) -> wire.ReviewGate:
    opened = gate.gate if isinstance(gate, w.RearmingGate) else gate
    remediation = opened.remediation
    remedy = (wire.ReviewNoRemediation(kind="none_authorized", reason=remediation.reason)
              if isinstance(remediation, w.NoAuthorizedRemediation) else
              wire.ReviewAttemptedRemediation(kind="attempted", operation=remediation.operation.value,
                                              evidence_ids=list(remediation.evidence_ids),
                                              outcome=remediation.outcome.value))
    actions = [wire.ReviewActionOffered(state="offered", operation=action.command.value, effect=action.effect)
               if isinstance(action, ActionOffered) else
               wire.ReviewActionRefused(state="refused", operation=action.command.value, reason=action.reason)
               for action in review_gate_actions(view, gate)]
    pending = next((item for item in view.retry_pending if item.gate_id == opened.gate_id), None)
    return wire.ReviewGate(
        gate_id=opened.gate_id, state="rearming" if isinstance(gate, w.RearmingGate) else "open",
        opened_generation=opened.opened_generation, cause=_cause(opened.cause, opened, view), remediation=remedy,
        actions=actions,
        pending_retry=None if pending is None else wire.ReviewPendingRetry(
            authorization_id=pending.authorization_id, command_id=pending.command_id),
    )


def _dispatch(decision: RecordedDispatch) -> wire.ReviewDispatch:
    if isinstance(decision, ReviewTerminal):
        return wire.ReviewDispatchTerminal(kind="terminal", state=decision.state.value)
    if isinstance(decision, ReviewNotAdmitted):
        return wire.ReviewDispatchNotAdmitted(kind="not_admitted", holds=list(decision.holds))
    if isinstance(decision, ReviewDeferred):
        return wire.ReviewDispatchDeferred(kind="deferred", reason=decision.reason)
    if isinstance(decision, ReviewHeld):
        return wire.ReviewDispatchHeld(kind="held", gate_ids=list(decision.gate_ids))
    if isinstance(decision, SubmissionUnresolved):
        return wire.ReviewDispatchSubmissionUnresolved(kind="submission_unresolved",
                                                       command_ids=list(decision.command_ids))
    if isinstance(decision, ReleaseProjectionPending):
        return wire.ReviewDispatchReleasePending(kind="release_projection_pending", command_id=decision.command_id)
    if isinstance(decision, ReviewOwed):
        return wire.ReviewDispatchOwed(kind="owed", review="initial" if decision.kind is ReviewKind.INITIAL
                                       else "re_review")
    if isinstance(decision, ReviewWithheld):
        return wire.ReviewDispatchWithheld(
            kind="withheld", review="initial" if decision.kind is ReviewKind.INITIAL else "re_review",
            reason=decision.reason.value, detail=decision.detail)
    if isinstance(decision, ReviewInFlight):
        return wire.ReviewDispatchInFlight(
            kind="in_flight", review="initial" if decision.kind is ReviewKind.INITIAL else "re_review")
    assert isinstance(decision, ReviewNotOwed)
    return wire.ReviewDispatchNotOwed(kind="not_owed", reason=decision.reason)


def _daemon(view: ReviewControlView) -> wire.ReviewDaemonDecision:
    """The review daemon's own recorded decision, rendered as recorded; nothing here decides dispatch."""
    record = view.decision
    if record is None:
        return wire.ReviewDaemonUnrecorded(state="unrecorded")
    if not isinstance(record, RecordedReviewDecision):
        return wire.ReviewDaemonUnreadable(state="unreadable", reason=record.reason)
    basis = record.basis
    failure = record.read_failure
    return wire.ReviewDaemonRecorded(
        state="recorded", dispatch=_dispatch(record.decision), read=record.read.value,
        decided_at=record.decided_at.isoformat(), as_of=record.as_of.isoformat(),
        read_failure=None if failure is None else wire.ReviewReadFailure(
            failed_reads=failure.failed_reads, reason=failure.reason,
            first_failed_at=failure.first_failed_at.isoformat(), last_failed_at=failure.last_failed_at.isoformat()),
        pull_request=None if basis is None else wire.ReviewDecisionBasis(
            head_sha=basis.head_sha, state=basis.state.value, labels=sorted(label.value for label in basis.labels),
            external_holds=list(basis.external_holds)),
    )


def _continuation(view: ReviewControlView) -> wire.ReviewContinuation:
    continuation = view.snapshot.episode.continuation
    if isinstance(continuation, FreshReviewRequired):
        return wire.ReviewFreshReviewRequired(kind="fresh_review_required",
                                              owed_since_generation=continuation.owed_since_generation)
    source = continuation.source
    return wire.ReviewReviewedBaseline(
        kind="reviewed_baseline", head_sha=continuation.head_sha, patch_id=continuation.patch_id,
        reviewed_generation=continuation.reviewed_generation,
        source=wire.ReviewBoundSubmission(kind="bound_submission", command_id=source.command_id,
                                          generation=source.binding.fence.generation),
    )


def unavailable_review_control(error: BaseException) -> wire.ReviewUnavailableControl:
    """A failed authority read is unavailable, never an empty or absent gate set."""
    return wire.ReviewUnavailableControl(status="unavailable", reason=control_error(error))


def _execution(view: ReviewControlView) -> wire.ReviewExecution:
    execution = view.snapshot.execution
    if not isinstance(execution, CurrentReviewExecution):
        return wire.ReviewExecutionInactive()
    choices = [wire.ReviewGate(gate_id=gate.gate_id, state="open", opened_generation=gate.opened_generation,
        cause=_cause(gate.cause, gate, view),
        remediation=wire.ReviewNoRemediation(kind="none_authorized", reason="Only the selected fixed recovery is authorized."),
        actions=[wire.ReviewActionOffered(state="offered", operation=action.command.value, effect=action.effect)
                 if isinstance(action, ActionOffered) else wire.ReviewActionRefused(
                     state="refused", operation=action.command.value, reason=action.reason)], pending_retry=None)
        for gate, action in execution_recovery_choices(view)]
    return wire.ReviewExecutionCurrent(kind="current", execution_epoch=execution.binding.execution_epoch,
        execution_episode=execution.binding.execution_episode, failures=execution.state.failures,
        failure_ceiling=view.execution_ceiling, pin_deferrals=execution.state.pin_deferrals,
        displayed_round=execution.state.displayed_round, recovery_choices=choices)


def unavailable_review_command(error: BaseException) -> wire.ReviewCommandResult:
    """The stable request identity makes an uncertain response safe to replay."""
    return wire.ReviewCommandResult(
        outcome="unavailable", reason=control_error(error),
        control=wire.ReviewUnavailableControl(status="unavailable", reason="control read unavailable"))


def read_review_control(
    control: SQLiteReviewWorkflowControl, pr: int, request_id: str | None = None,
) -> wire.ReviewControl:
    key = WorkflowKey(control.writer.repository, PullRequestRef(pr))
    try:
        recovery = recovery_control(control._control.read_pr_repair(key))
    except Exception as error:
        recovery = RecoveryUnavailable(status="unavailable", reason=control_error(error))
    result = control.review_control_view(key, request_id=request_id)
    if isinstance(result, w.WorkflowUnavailable):
        return wire.ReviewUnavailableControl(status="unavailable", reason=result.reason, fix_recovery=recovery)
    if isinstance(result, MigrationRequiredReviewControl):
        return wire.ReviewMigrationRequiredControl(
            status="migration_required", repository=key.repository, pull_request=pr,
            active_families=[family.value for family in result.active_families], fix_recovery=recovery)
    if isinstance(result, AbsentReviewControl):
        return wire.ReviewAbsentControl(status="absent", repository=key.repository, pull_request=pr,
                                        epoch=result.authority.epoch, fix_recovery=recovery)
    assert isinstance(result, ReviewControlView)
    state = result.snapshot
    observation = result.observation
    return wire.ReviewAvailableControl(
        fix_recovery=recovery,
        execution=_execution(result),
        status="available",
        expected=wire.ReviewExpected(
            repository=key.repository, pull_request=pr, family="review_rejections", epoch=state.authority.epoch,
            generation=state.revision.fence.generation, version=state.revision.version,
            episode_id=state.episode.episode_id),
        workflow_held=bool(state.gates), gates=[_gate(gate, result) for gate in state.gates],
        guidance=state.episode.guidance, continuation=_continuation(result), daemon=_daemon(result),
        current_intent=_rearm(state.intent, result), requested_id=result.requested_id,
        requested_intent=_rearm(result.requested, result),
        observation=(wire.ReviewObservation(unresolved_since=observation.history.unresolved_since.isoformat(),
                                            next_read_at=observation.history.next_read_at.isoformat(),
                                            failed_reads=observation.history.failed_reads,
                                            last_error=observation.last_error)
                     if observation is not None else None),
    )


def submit_review_control(
    control: SQLiteReviewWorkflowControl, pr: int, body: wire.ReviewCommandBody,
) -> wire.ReviewCommandResult:
    command = body.command()
    key = WorkflowKey(control.writer.repository, PullRequestRef(pr))
    binding = body.expected.binding()
    if binding.fence.key != key:
        return wire.ReviewCommandResult(outcome="rejected", reason="command target differs from the configured deployment",
                                        control=read_review_control(control, pr))
    current = control.load_workflow(key)
    if isinstance(current, w.WorkflowUnavailable):
        return wire.ReviewCommandResult(outcome="unavailable", reason=current.reason,
                                        control=read_review_control(control, pr))
    if isinstance(current, (w.WorkflowMigrationRequired, w.WorkflowAbsent)):
        reason = ("review rejection control is not active for this repository"
                  if isinstance(current, w.WorkflowMigrationRequired)
                  else "no review workflow is recorded for this pull request")
        return wire.ReviewCommandResult(outcome="rejected", reason=reason, control=read_review_control(control, pr))
    # Acceptance validates epoch/episode inside its transaction. Exact replay
    # may legitimately name the episode the accepted re-arm replaced.
    # This subject names the trusted local operator account, not an HTTP user
    # or User-Agent claim. The server's loopback/Origin/CSRF boundary is required.
    actor = w.OperatorActor(f"local-user:{os.getuid()}", "dashboard")
    if isinstance(command, w.CancelRearm):
        result = control.cancel_rearm(command, actor)
    elif isinstance(command, w.AuthorizeAbsentRetry):
        result = control.authorize_retry(command, actor)
    else:
        result = control.request_rearm(command, actor)
    view = read_review_control(control, pr, wire.requested_rearm(body))
    if isinstance(result, w.WorkflowCommitted):
        return wire.ReviewCommandResult(outcome="replayed" if result.idempotent else "accepted", reason="", control=view)
    if isinstance(result, w.WorkflowConflict):
        return wire.ReviewCommandResult(outcome="conflict", reason=result.reason, control=view)
    return wire.ReviewCommandResult(
        outcome="unavailable" if result.code is w.WorkflowRejection.OBSERVATION_UNAVAILABLE else "rejected",
        reason=result.reason, control=view)
