"""Pending review re-arm recovery and the review control read model; ADR 0015.

Mirrors `resolve_rearm` without workspace handoffs. Observation scheduling is
attached to the existing intent (`RearmObservation`), never a work queue; a
bounded run of unavailable or undecidable reads opens one visible
`EvidenceUnavailable` hold (ADR 0012). Values and functions perform no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum

from .contracts import CommandId
from .resolve_rearm import PendingRetry, RearmObservation
from .review_dispatch_record import RecordedReviewDecision
from .review_workflow import (
    CancelledReviewRearm, CompleteReviews, CompletedReviewRearm, ObservedPullRequest, PendingReviewRearm, ReviewRearmIntent,
    ReviewRearmPreflight, ReviewWorkflowSnapshot, SupersededReviewRearm,
)
from .review_workflow_policy import (
    OBSERVATION_MAX_AGE_SECONDS, ceiling_reached_again, complete_review_rearm, review_projection_obsolete, review_rearm_observation_gate_id,
    review_rearm_stop_reason, supersede_review_rearm,
)
from .labels import Label
from .workflow import (
    EvidenceUnavailable, NoAuthorizedRemediation, OpenGate, RearmingGate, ReconciliationPolicy, RemoteOutcomeUncertain,
    RetryAuthorizationRequired, StopReason, WorkflowRejected, WorkflowRejection, WorkflowTransition, WorkflowUnchanged,
)
from .workflow_identity import (
    MAX_DETAIL_CHARS, MAX_WORKFLOW_GATES, FamilyAuthority, WorkflowFamily, WorkflowKey, validate_workflow_identifier,
    validate_workflow_text,
)


@dataclass(frozen=True)
class PendingReviewRecovery:
    """A due pending review re-arm with its exact observation authority."""
    snapshot: ReviewWorkflowSnapshot
    observation: RearmObservation

    def __post_init__(self) -> None:
        if (not isinstance(self.snapshot, ReviewWorkflowSnapshot)
                or not isinstance(self.snapshot.intent, PendingReviewRearm)
                or not isinstance(self.observation, RearmObservation)
                or self.observation.request_id != self.snapshot.intent.request_id
                or self.observation.binding != self.snapshot.binding):
            raise ValueError("pending review recovery requires its exact re-arm observation authority")


def update_review_rearm_observation(
    snapshot: ReviewWorkflowSnapshot, observation: RearmObservation | None, *, now: datetime,
    policy: ReconciliationPolicy = ReconciliationPolicy(),
) -> ReviewWorkflowSnapshot:
    """Only a bounded run of failed reads adds the hold; a successful read removes it."""
    if not isinstance(snapshot, ReviewWorkflowSnapshot) or not isinstance(snapshot.intent, PendingReviewRearm):
        raise ValueError("review re-arm observation requires a current pending intent")
    if not isinstance(policy, ReconciliationPolicy):
        raise ValueError("review re-arm observation requires a validated retry policy")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("review re-arm observation requires an aware clock")
    intent = snapshot.intent
    if observation is not None and (not isinstance(observation, RearmObservation)
                                    or observation.request_id != intent.request_id
                                    or observation.binding != snapshot.binding):
        raise ValueError("review re-arm observation belongs to another intent or authority")
    gate_id = review_rearm_observation_gate_id(intent.request_id)
    prior_gate = next((g for g in snapshot.gates if isinstance(g, OpenGate) and g.gate_id == gate_id), None)
    gates = tuple(g for g in snapshot.gates if (g.gate if isinstance(g, RearmingGate) else g).gate_id != gate_id)
    if observation is not None and observation.last_error:
        history = observation.history
        if now < history.unresolved_since:
            raise ValueError("review re-arm observation precedes its retained history")
        if (prior_gate is not None or history.failed_reads >= policy.maximum_reads
                or (now - history.unresolved_since).total_seconds() >= policy.maximum_age_seconds):
            gates += (OpenGate(
                gate_id, prior_gate.opened_generation if prior_gate else snapshot.revision.fence.generation,
                EvidenceUnavailable(f"review re-arm reconciliation for request {intent.request_id}",
                                    history.unresolved_since, history.failed_reads, observation.last_error[:16384]),
                NoAuthorizedRemediation("a fresh complete pull request read is required; the operator may cancel"),
            ),)
    if gates == snapshot.gates:
        return snapshot
    return replace(snapshot, gates=gates, revision=replace(snapshot.revision, version=snapshot.revision.version + 1))


def decide_review_rearm_reconciliation(
    snapshot: ReviewWorkflowSnapshot, accepted: ReviewRearmPreflight, pr: ObservedPullRequest, *,
    verified_commands: tuple[CommandId, ...], rejected_commands: tuple[CommandId, ...],
    uncertainty: tuple[RemoteOutcomeUncertain, ...], evidence_id: str, now: datetime, ceiling: int,
    trigger_label: Label, external_holds: tuple[str, ...] = (),
) -> WorkflowTransition:
    """Join receipts with one fresh observation: supersede, complete or keep waiting.

    Head, state and labels decide supersession from any present observation;
    only a complete review read can complete. A refused completion is
    returned as-is so the caller counts it as an undecidable read (bounded,
    then visible), never as silent waiting.
    """
    if not isinstance(snapshot, ReviewWorkflowSnapshot) or not isinstance(accepted, ReviewRearmPreflight):
        raise ValueError("review re-arm reconciliation requires its snapshot and accepted preflight")
    if not isinstance(pr, ObservedPullRequest):
        raise ValueError("review re-arm reconciliation requires a present pull request observation")
    key = snapshot.revision.fence.key
    if pr.key != key or accepted.pr_key != key:
        raise ValueError("review re-arm observations belong to another workflow")
    if not isinstance(now, datetime) or now.utcoffset() is None or not (
        0 <= (now - pr.observed_at).total_seconds() <= OBSERVATION_MAX_AGE_SECONDS
    ):
        raise ValueError("review re-arm observation must be fresh and not from the future")
    validate_workflow_identifier(evidence_id, "review re-arm observation evidence")
    pending = snapshot.intent
    if not isinstance(pending, PendingReviewRearm):
        return WorkflowUnchanged(snapshot)
    required = set(pending.required_commands)
    for group in (verified_commands, rejected_commands):
        if not isinstance(group, tuple) or len(set(group)) != len(group) or not set(group) <= required:
            raise ValueError("receipt evidence must name unique required review re-arm commands")
    if set(verified_commands) & set(rejected_commands):
        raise ValueError("a required command cannot be both verified and rejected")
    if not isinstance(uncertainty, tuple) or any(not isinstance(item, RemoteOutcomeUncertain) for item in uncertainty):
        raise ValueError("review re-arm uncertainty requires typed command observations")
    reason = review_rearm_stop_reason(accepted, pr, external_holds=external_holds)
    if reason is None and review_projection_obsolete(snapshot, external_holds):
        reason = StopReason.DECISION_CHANGED
    if reason is None and pr.observed_at >= accepted.observed_at and ceiling_reached_again(pending, pr, ceiling):
        reason = StopReason.CEILING_REACHED_AGAIN
    if reason is None and rejected_commands:
        if not isinstance(pr.reviews, CompleteReviews) or pr.observed_at < accepted.observed_at:
            # A refusal is cited only with a fresh complete read after acceptance;
            # anything less is an undecidable read (bounded, then visible).
            return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE,
                                    "an effect-rejected supersession requires a complete review read")
        reason = StopReason.REQUIRED_EFFECT_REJECTED
    if reason is not None:
        return supersede_review_rearm(snapshot, reason, evidence_id,
                                      verified_commands=verified_commands, uncertainty=uncertainty)
    if set(verified_commands) != required:
        return WorkflowUnchanged(snapshot)
    return complete_review_rearm(snapshot, pr, verified_commands=verified_commands, evidence_ids=(evidence_id,),
                                 now=now, ceiling=ceiling, trigger_label=trigger_label, external_holds=external_holds)


class ReleaseStatus(str, Enum):
    PENDING = "pending"      # no SUCCEEDED receipt yet: dispatch waits
    VERIFIED = "verified"    # the release relabel succeeded, or a fresh read showed its labels
    REFUSED = "refused"      # the relabel's guard refused it: a visible gate holds


@dataclass(frozen=True)
class ReleaseProjection:
    """The current generation's RELEASE_CEILING projection, from the store's own receipts."""
    command_id: CommandId
    status: ReleaseStatus

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "release projection command")
        if not isinstance(self.status, ReleaseStatus):
            raise ValueError("release projection status must be typed")

    @property
    def verified(self) -> bool:
        return self.status is ReleaseStatus.VERIFIED


@dataclass(frozen=True)
class MigrationRequiredReviewControl:
    """The review family requires explicit activation before any review work.

    No lever exists for this pull request's review holds until the review
    family is activated (plan decision 2: never two authorities, no fallback).
    """
    key: WorkflowKey
    active_families: tuple[WorkflowFamily, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey):
            raise ValueError("migration-required review control requires a queried workflow")
        if (not isinstance(self.active_families, tuple)
                or any(not isinstance(family, WorkflowFamily) or family is WorkflowFamily.REVIEW_REJECTIONS
                       for family in self.active_families)
                or len(set(self.active_families)) != len(self.active_families)):
            raise ValueError("migration-required review control names the other active families")


@dataclass(frozen=True)
class AbsentReviewControl:
    key: WorkflowKey
    authority: FamilyAuthority

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey) or not isinstance(self.authority, FamilyAuthority):
            raise ValueError("absent review control requires its active family authority")


# The harness restores an open ceiling's hold at most MAX_HOLD_RESTORES times;
# a stored count is bounded far above that so corruption is refused, not shown.
_MAX_RESTORE_COUNT = 2**31


@dataclass(frozen=True)
class HoldRestoration:
    """How often the harness re-projected an open ceiling's missing hold, and whether it gave up (contested)."""
    gate_id: str
    restores: int
    contested: bool

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.gate_id, "restored gate")
        if type(self.restores) is not int or not 0 <= self.restores <= _MAX_RESTORE_COUNT:
            raise ValueError("hold restorations are a bounded non-negative count")
        if not isinstance(self.contested, bool):
            raise ValueError("a hold contest is recorded as a boolean fact")


@dataclass(frozen=True)
class UnreadableHoldRestoration:
    """The harness's restoration record for this ceiling cannot be read; the gate and its lever are unaffected."""
    gate_id: str
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.gate_id, "restored gate")
        validate_workflow_text(self.reason, "unreadable restoration", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class GateFactsUnavailable:
    """The store's own facts about this gate (retry grants, obligation receipts) disagree; nothing is offered on it.

    Only the view degrades: the gate still holds and reconciliation, which
    reads the same facts strictly, is unchanged.
    """
    gate_id: str
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.gate_id, "gate with unavailable facts")
        validate_workflow_text(self.reason, "unavailable gate facts", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class UnreadableReviewDecision:
    """The review daemon's recorded decision for this pull request is malformed."""
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "unreadable review decision", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class RetryRefusal:
    """Why the shared grant policy would refuse a retry authorization for this gate now."""
    gate_id: str
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.gate_id, "retry gate")
        validate_workflow_text(self.reason, "retry refusal", limit=MAX_DETAIL_CHARS)


class RearmCommandStatus(str, Enum):
    PENDING = "pending"          # not yet settled: queued or executing before any expiry
    VERIFIED = "verified"        # its SUCCEEDED receipt
    REFUSED = "refused"          # a verified refusal (the command's guard no longer held)
    UNCERTAIN = "uncertain"      # outcome unknown; bounded reconciliation continues
    HELD = "held"                # reconciliation-only under its own visible gate
    UNAVAILABLE = "unavailable"  # the store's facts about this command disagree (view only)


@dataclass(frozen=True)
class RearmCommandProgress:
    command_id: CommandId
    status: RearmCommandStatus

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "re-arm command")
        if not isinstance(self.status, RearmCommandStatus):
            raise ValueError("re-arm command status must be typed")


def _unique_gates(items: tuple, name: str) -> set[str]:
    gates = [item.gate_id for item in items]
    if len(set(gates)) != len(gates):
        raise ValueError(f"{name} name each gate at most once")
    return set(gates)


@dataclass(frozen=True)
class ReviewControlView:
    """One read snapshot of a PR's review control; inspection never schedules or writes.

    Every field is a harness-owned fact read in one SQLite snapshot.
    `decision` is the review daemon's own recorded dispatch decision (with
    the read it decided from and its as-of time), never re-derived here.
    """
    snapshot: ReviewWorkflowSnapshot
    requested_id: str | None
    requested: ReviewRearmIntent | None
    observation: RearmObservation | None
    release: ReleaseProjection | None = None
    # Retry gates the shared grant policy would admit now: offered exactly
    # when acceptance would succeed, never a grant the executor must discard.
    retry_authorizable: frozenset[str] = frozenset()
    retry_refusals: tuple[RetryRefusal, ...] = ()
    retry_pending: tuple[PendingRetry, ...] = ()
    decision: RecordedReviewDecision | UnreadableReviewDecision | None = None
    hold_restorations: tuple[HoldRestoration | UnreadableHoldRestoration, ...] = ()
    review_runs_active: bool = False
    command_progress: tuple[RearmCommandProgress, ...] = ()
    gate_facts_unavailable: tuple[GateFactsUnavailable, ...] = ()
    # The configured review trigger acceptance plans a release with; the view
    # describes a re-arm with the same kept-holds predicate acceptance uses.
    trigger_label: Label = field(kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ReviewWorkflowSnapshot):
            raise ValueError("review control view requires current workflow authority")
        if not isinstance(self.trigger_label, Label) or self.trigger_label is Label.NEEDS_HUMAN_REVIEW:
            raise ValueError("the review trigger is a label other than the attention hold")
        if self.requested_id is not None:
            validate_workflow_identifier(self.requested_id, "requested re-arm")
        if self.requested is not None and (
            not isinstance(self.requested, (PendingReviewRearm, CompletedReviewRearm, SupersededReviewRearm,
                                            CancelledReviewRearm))
            or self.requested.request_id != self.requested_id
        ):
            raise ValueError("requested review re-arm must belong to this exact request")
        intent = self.snapshot.intent
        if isinstance(intent, PendingReviewRearm):
            if self.observation is None:
                raise ValueError("pending review control requires its observation history")
            PendingReviewRecovery(self.snapshot, self.observation)
        elif self.observation is not None:
            raise ValueError("only current pending control carries observation history")
        if self.release is not None and not isinstance(self.release, ReleaseProjection):
            raise ValueError("release projection must be typed")
        gates = {(g.gate if isinstance(g, RearmingGate) else g).gate_id for g in self.snapshot.gates}
        retry_gates = {g.gate_id for g in self.snapshot.gates
                       if isinstance(g, OpenGate) and isinstance(g.cause, RetryAuthorizationRequired)}
        if not isinstance(self.retry_authorizable, frozenset) or any(
            not isinstance(gate_id, str) for gate_id in self.retry_authorizable
        ):
            raise ValueError("retry-authorizable gates must be a frozenset of gate identities")
        if not self.retry_authorizable <= gates:
            raise ValueError("only open gates can offer a retry authorization")
        for name, items, kind in (("retry refusals", self.retry_refusals, RetryRefusal),
                                  ("pending retries", self.retry_pending, PendingRetry),
                                  ("hold restorations", self.hold_restorations,
                                   (HoldRestoration, UnreadableHoldRestoration)),
                                  ("re-arm command progress", self.command_progress, RearmCommandProgress),
                                  ("gates with unavailable facts", self.gate_facts_unavailable,
                                   GateFactsUnavailable)):
            if not isinstance(items, tuple) or len(items) > MAX_WORKFLOW_GATES * 2 or any(
                not isinstance(item, kind) for item in items
            ):
                raise ValueError(f"{name} must be a bounded tuple of typed facts")
        refused = _unique_gates(self.retry_refusals, "retry refusals")
        if not refused <= retry_gates or refused & self.retry_authorizable:
            raise ValueError("a retry refusal explains one retry gate that is not offered")
        waiting = _unique_gates(self.retry_pending, "pending retries")
        if not waiting <= gates or waiting & self.retry_authorizable:
            raise ValueError("a pending retry names one visible gate that is not also offered")
        if not _unique_gates(self.hold_restorations, "hold restorations") <= gates:
            raise ValueError("hold restorations belong to visible gates")
        unavailable = _unique_gates(self.gate_facts_unavailable, "gates with unavailable facts")
        if not unavailable <= gates or unavailable & (self.retry_authorizable | refused | waiting):
            raise ValueError("a gate whose facts are unavailable is visible and neither offered, refused nor waiting")
        if isinstance(self.decision, RecordedReviewDecision):
            if self.decision.key != self.snapshot.revision.fence.key:
                raise ValueError("the recorded review decision belongs to this workflow")
        elif self.decision is not None and not isinstance(self.decision, UnreadableReviewDecision):
            raise ValueError("the review decision is recorded, unreadable or absent")
        if not isinstance(self.review_runs_active, bool):
            raise ValueError("whether a review run is live is a boolean fact")
        progress = tuple(item.command_id for item in self.command_progress)
        expected = intent.required_commands if isinstance(intent, PendingReviewRearm) else ()
        if progress and progress != expected:
            raise ValueError("re-arm command progress follows exactly the pending intent's obligations")


ReviewControlRead = MigrationRequiredReviewControl | AbsentReviewControl | ReviewControlView
