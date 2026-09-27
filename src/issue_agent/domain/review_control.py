"""What an operator may do about a pull request's review holds; ADR 0015.

A pure projection of `ReviewControlView`, the review owner's one-snapshot
read. An action that acceptance would refuse for a reason the snapshot
already proves is returned refused with that reason, so the operator surface
never offers a command only to have it conflict or be rejected for something
knowable. A pull request the review daemon recorded merged offers nothing;
one it recorded closed keeps its commands (a reopen needs them) and says that
acceptance refuses while it stays closed. Other remote facts (a moved head,
reviews counted since the gate opened) stay acceptance's to refuse. Dispatch
is never decided here: the view carries the review daemon's own recorded
decision.
Values and functions perform no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import CommandId
from .labels import Label
from .review import labels_holding_review
from .review_dispatch_record import RecordedReviewDecision, ReviewTerminal
from .review_rejection import PullRequestState
from .review_evidence import ReviewEvidenceKind, review_evidence_gate_kind
from .review_rearm import ReviewControlView
from .review_workflow import (
    CancelledReviewRearm, CountedReview, PendingReviewRearm, ReviewProjectionPlan,
    ReviewProjectionRefused, ReviewRejectionCeiling, ReviewWorkflowSnapshot, SupersededReviewRearm,
)
from .review_workflow_policy import (
    REVIEW_REARM_OBSERVATION_GATE_PREFIX, RearmKeptHolds, continue_rearm_kept_holds, request_rearm_kept_holds,
    review_ceiling_rearm_refusal, review_dismissal_command_id, review_release_command_id,
)
from .workflow import EvidenceUnavailable, OpenGate, RearmNeedsDecision, RearmingGate, RetryAuthorizationRequired
from .workflow_reconciliation import is_prewrite_gate


class ReviewOperatorCommand(str, Enum):
    REQUEST_REARM = "request_rearm"
    CONTINUE_REARM = "continue_rearm"
    CANCEL_REARM = "cancel_rearm"
    AUTHORIZE_RETRY = "authorize_retry"


@dataclass(frozen=True)
class ActionOffered:
    """Acceptance would admit this command now; `effect` is what accepting it records."""
    command: ReviewOperatorCommand
    effect: str


@dataclass(frozen=True)
class ActionRefused:
    """Acceptance would refuse this command for the stated, already-proven reason."""
    command: ReviewOperatorCommand
    reason: str


GateAction = ActionOffered | ActionRefused

# Commands whose acceptance re-reads the pull request (and refuses one that is not open).
_REREADS = frozenset({ReviewOperatorCommand.REQUEST_REARM, ReviewOperatorCommand.CONTINUE_REARM,
                      ReviewOperatorCommand.AUTHORIZE_RETRY})

_DRAINING = ("a review run for this pull request is still live; it must drain before a re-arm is accepted "
             "(refresh after it settles)")



def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _opened_id(gate: OpenGate | RearmingGate) -> str:
    return (gate.gate if isinstance(gate, RearmingGate) else gate).gate_id


def _kept_holds(view: ReviewControlView, gate: OpenGate) -> tuple[str, ...]:
    """Holds a re-arm of `gate` keeps, so its relabel keeps needs-human-review.

    Exactly acceptance's predicate (`request_rearm_kept_holds`), applied to
    the trusted external holds of the daemon's last read: acceptance applies
    it to its own fresh read. The needs-human-review a gate projects is never
    one of them.
    """
    external = _external(view)
    return _named(request_rearm_kept_holds(view.snapshot, gate.gate_id, external, view.trigger_label))


def _external(view: ReviewControlView) -> tuple[str, ...]:
    decision = view.decision
    return (decision.basis.external_holds
            if isinstance(decision, RecordedReviewDecision) and decision.basis is not None else ())


def _named(kept: RearmKeptHolds) -> tuple[str, ...]:
    return ((_plural(len(kept.gates), "other open gate"),) if kept.gates else ()) + kept.external


def _dispatch_exclusions(view: ReviewControlView, kept: tuple[str, ...]) -> tuple[str, ...]:
    """Labels of the daemon's last read that still keep review dispatch off once the relabel removes the hold."""
    decision = view.decision
    if not isinstance(decision, RecordedReviewDecision) or decision.basis is None:
        return ()
    relabelled = decision.basis.labels - {Label.NEEDS_HUMAN_REVIEW}
    return tuple(label for label in labels_holding_review(relabelled) if label not in kept)


def _as_of(view: ReviewControlView) -> str:
    decision = view.decision
    return (f" as of the review daemon's read at {decision.as_of.isoformat()}"
            if isinstance(decision, RecordedReviewDecision) else "")


def _request(view: ReviewControlView, gate: OpenGate) -> GateAction:
    snapshot = view.snapshot
    intent = snapshot.intent
    command = ReviewOperatorCommand.REQUEST_REARM
    cause = gate.cause
    assert isinstance(cause, ReviewRejectionCeiling)
    if cause.episode_id != snapshot.episode.episode_id:
        return ActionRefused(command, "this ceiling belongs to another review episode; it cannot be re-armed")
    if isinstance(intent, PendingReviewRearm):
        return ActionRefused(command, f"review re-arm {intent.request_id} already owns this workflow; "
                                      "cancel it or wait for it to finish")
    refusal = review_ceiling_rearm_refusal(gate)
    if refusal is not None:
        return ActionRefused(command, refusal.reason)
    if view.review_runs_active:
        return ActionRefused(command, _DRAINING)
    replaces = (f" It replaces stopped request {intent.request_id}."
                if isinstance(intent, (CancelledReviewRearm, SupersededReviewRearm)) else "")
    authorizes = (f"Authorizes dismissal of exactly the {_plural(len(cause.counted), 'counted bot review')} listed "
                  "(never a human or later review).")
    kept = _kept_holds(view, gate)
    if kept:
        return ActionOffered(command, (
            f"{authorizes} It buys no review: holds a re-arm keeps remain ({', '.join(kept)}){_as_of(view)}, so "
            "the relabel keeps needs-human-review until they clear. Acceptance re-reads GitHub and plans the relabel "
            "from what it finds." + replaces))
    excluded = _dispatch_exclusions(view, kept)
    if excluded:
        return ActionOffered(command, (
            f"{authorizes} Once the dismissals are verified the relabel returns the pull request to review, but "
            f"review dispatch still skips it for {', '.join(excluded)}{_as_of(view)}: no review is bought until "
            "they are removed. Acceptance re-reads GitHub first." + replaces))
    return ActionOffered(command, (
        f"{authorizes} Once the dismissals are verified the pull request returns to review and one fresh review is "
        "owed, unless acceptance's fresh read finds a hold outside this ceiling." + replaces))


def _continue(view: ReviewControlView, gate: OpenGate) -> tuple[GateAction, ...]:
    intent = view.snapshot.intent
    cause = gate.cause
    assert isinstance(cause, RearmNeedsDecision)
    if not isinstance(intent, (CancelledReviewRearm, SupersededReviewRearm)) or intent.request_id != cause.stopped_request_id:
        return ()
    command = ReviewOperatorCommand.CONTINUE_REARM
    if view.review_runs_active:
        return (ActionRefused(command, _DRAINING),)
    kept = _named(continue_rearm_kept_holds(view.snapshot, gate.gate_id, _external(view), view.trigger_label))
    outcome = (f" It buys no review: holds a re-arm keeps remain ({', '.join(kept)}){_as_of(view)}, so the relabel "
               "keeps needs-human-review until they clear." if kept else
               " Once its dismissals are verified the pull request returns to review and one fresh review is owed, "
               "unless acceptance's fresh read finds a hold outside this ceiling.")
    return (ActionOffered(command, (
        f"Re-plans stopped request {intent.request_id}'s remaining authorized dismissals against a fresh read. "
        "It authorizes no new dismissal, repeats no verified effect and renews no allowance." + outcome)),)


def _retry(view: ReviewControlView, gate: OpenGate) -> GateAction:
    command = ReviewOperatorCommand.AUTHORIZE_RETRY
    cause = gate.cause
    assert isinstance(cause, RetryAuthorizationRequired)
    if gate.gate_id in view.retry_authorizable:
        return ActionOffered(command, (
            f"Permits exactly one more execution of command {cause.command_id} under its current generation. The "
            "executor re-reads before writing; nothing is written if the effect has appeared meanwhile."))
    waiting = next((item for item in view.retry_pending if item.gate_id == gate.gate_id), None)
    if waiting is not None:
        return ActionRefused(command, f"authorization {waiting.authorization_id} is already waiting for the executor")
    refusal = next((item.reason for item in view.retry_refusals if item.gate_id == gate.gate_id), None)
    return ActionRefused(command, refusal or "the harness would not accept a retry authorization for this gate now")


def _terminal(view: ReviewControlView) -> tuple[PullRequestState, str] | None:
    decision = view.decision
    if isinstance(decision, RecordedReviewDecision) and isinstance(decision.decision, ReviewTerminal):
        return decision.decision.state, decision.as_of.isoformat()
    return None


def _unavailable(view: ReviewControlView, gate: OpenGate | RearmingGate) -> str | None:
    """Why nothing can be offered on this gate whatever it holds, if anything proves that."""
    terminal = _terminal(view)
    if terminal is not None and terminal[0] is PullRequestState.MERGED:
        return (f"the review daemon found this pull request merged (as of {terminal[1]}): a merged pull request is "
                "never reviewed again, so no review-control command is offered; its recorded holds are left as they are")
    facts = next((item for item in view.gate_facts_unavailable if item.gate_id == _opened_id(gate)), None)
    if facts is not None:
        return f"the harness's records for this gate disagree ({facts.reason}); nothing is accepted on it until they agree"
    return None


def review_gate_actions(view: ReviewControlView, gate: OpenGate | RearmingGate) -> tuple[GateAction, ...]:
    """Every operator command that applies to this gate, offered or refused with its reason.

    A gate with no operator command (outcome uncertainty, unavailable
    evidence, a refused projection) ends only through harness reconciliation
    or a corrective human change on GitHub that the next read verifies. A
    merged pull request, or a gate whose own facts disagree, offers nothing;
    a closed one keeps its commands, saying acceptance refuses while closed.
    """
    if not isinstance(view, ReviewControlView):
        raise ValueError("operator actions are derived from a review control view")
    if not any(item == gate for item in view.snapshot.gates):
        raise ValueError("operator actions are derived for a gate of this view")
    actions = _gate_actions(view, gate)
    reason = _unavailable(view, gate)
    if reason is not None:
        return tuple(ActionRefused(action.command, reason) for action in actions)
    terminal = _terminal(view)
    if terminal is None:
        return actions
    # Closed, not merged: the gate is intact and a reopen needs its lever.
    # Only the commands whose acceptance re-reads GitHub refuse a closed pull request.
    closed = (f" The review daemon found this pull request closed (as of {terminal[1]}); acceptance re-reads GitHub "
              "and refuses while it stays closed.")
    return tuple(ActionOffered(action.command, action.effect + closed)
                 if isinstance(action, ActionOffered) and action.command in _REREADS else action
                 for action in actions)


def _gate_actions(view: ReviewControlView, gate: OpenGate | RearmingGate) -> tuple[GateAction, ...]:
    intent = view.snapshot.intent
    if isinstance(gate, RearmingGate):
        if isinstance(intent, PendingReviewRearm) and intent.request_id == gate.request_id:
            return (ActionOffered(ReviewOperatorCommand.CANCEL_REARM, (
                f"Stops request {intent.request_id}. Verified effects remain facts, its unexecuted obligations are "
                "retired and nothing is refunded; the ceiling stays held until a continuation or new request.")),)
        return ()
    cause = gate.cause
    if isinstance(cause, ReviewRejectionCeiling):
        return (_request(view, gate),)
    if isinstance(cause, RearmNeedsDecision):
        return _continue(view, gate)
    if isinstance(cause, RetryAuthorizationRequired):
        return (_retry(view, gate),)
    return ()


# -- typed hold kinds ------------------------------------------------------------------------------

class EvidenceHold(str, Enum):
    EXEMPTION_TRAILERS = "exemption_trailers"   # commit-trailer gate exemptions (ADR 0014) are unreadable
    REVIEW_HISTORY = "review_history"           # the complete review list is unreadable
    PREWRITE_READ = "prewrite_read"             # a command's guard could not be read before its write
    REARM_OBSERVATION = "rearm_observation"     # a pending re-arm's reconciliation reads keep failing
    OTHER = "other"


def evidence_hold(gate: OpenGate | RearmingGate) -> EvidenceHold | None:
    """Which bounded read an `EvidenceUnavailable` gate waits on; None for any other gate."""
    opened = gate.gate if isinstance(gate, RearmingGate) else gate
    if not isinstance(opened, OpenGate) or not isinstance(opened.cause, EvidenceUnavailable):
        return None
    kind = review_evidence_gate_kind(opened)
    if kind is ReviewEvidenceKind.EXEMPTION_TRAILERS:
        return EvidenceHold.EXEMPTION_TRAILERS
    if kind is ReviewEvidenceKind.REVIEW_HISTORY:
        return EvidenceHold.REVIEW_HISTORY
    if is_prewrite_gate(opened):
        return EvidenceHold.PREWRITE_READ
    if opened.gate_id.startswith(REVIEW_REARM_OBSERVATION_GATE_PREFIX):
        return EvidenceHold.REARM_OBSERVATION
    return EvidenceHold.OTHER


class RefusedProjection(str, Enum):
    CEILING_RELEASE = "ceiling_release"   # a disproven ceiling's release relabel was refused by its guard
    ESCALATION_HOLD = "escalation_hold"   # a verdict escalation's hold spent its bounded re-plans


def refused_projection(snapshot: ReviewWorkflowSnapshot, cause: ReviewProjectionRefused) -> RefusedProjection:
    """A release has exactly one command per generation; every other refused projection gate is an escalation's."""
    if not isinstance(snapshot, ReviewWorkflowSnapshot) or not isinstance(cause, ReviewProjectionRefused):
        raise ValueError("a refused projection is classified within its review workflow")
    key = snapshot.revision.fence.key
    if cause.generation >= 1 and cause.command_id == review_release_command_id(key, cause.generation):
        return RefusedProjection.CEILING_RELEASE
    return RefusedProjection.ESCALATION_HOLD


# -- pending obligations ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DismissalObligation:
    review: CountedReview


@dataclass(frozen=True)
class ProjectionObligation:
    plan: ReviewProjectionPlan


@dataclass(frozen=True)
class UnlabelledObligation:
    """A required command whose identity no planned obligation derives (never produced by the policy)."""


RearmObligation = DismissalObligation | ProjectionObligation | UnlabelledObligation


def rearm_obligations(
    snapshot: ReviewWorkflowSnapshot, intent: PendingReviewRearm,
) -> tuple[tuple[CommandId, RearmObligation], ...]:
    """Each required command of a pending re-arm with the obligation it discharges, in order."""
    if not isinstance(snapshot, ReviewWorkflowSnapshot) or not isinstance(intent, PendingReviewRearm):
        raise ValueError("obligations are read from a pending review re-arm")
    key = snapshot.revision.fence.key
    dismissals = {review_dismissal_command_id(key, intent.request_id, review.review_id): review
                  for review in intent.dismissals}
    labelled: list[tuple[CommandId, RearmObligation]] = []
    for command_id in intent.required_commands:
        if command_id == intent.projection.command_id:
            labelled.append((command_id, ProjectionObligation(intent.projection)))
        elif command_id in dismissals:
            labelled.append((command_id, DismissalObligation(dismissals[command_id])))
        else:
            labelled.append((command_id, UnlabelledObligation()))
    return tuple(labelled)
