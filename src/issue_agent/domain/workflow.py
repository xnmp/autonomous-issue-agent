"""Resolve workflow values and family-shared transition contracts; ADR 0015.

Adapters mint observations and operator provenance. These values perform no I/O.
Review-family values live in `review_workflow`; only the shared unions and
transition envelopes below admit them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from .contracts import CommandId, LifecycleCommand, WorkflowAttemptIdentity
from .workflow_identity import (
    MAX_DETAIL_CHARS, MAX_REARM_COMMANDS, MAX_WORKFLOW_GATES, FamilyAuthority, IssueRef, WorkflowBinding, WorkflowFamily,
    WorkflowFence, WorkflowKey, WorkflowRevision, WorkflowTarget, target_family,
    validate_workflow_identifier, validate_workflow_integer, validate_workflow_text,
)
from .review_rejection import ReviewExecutionCeiling, ReviewVerdictHold, ReviewProjectionRefused, ReviewRearmPreflight, ReviewRejectionCeiling
from .world import Comment, IssueView
from .labels import Label
from .resolve_split import NeedsAdjudication, SplitContinuation, SplitImplement, split_sources
from .resolve_workspace import (
    EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared, PrepareResolveWorkspace,
    ResolveWorkspaceObserved,
)

if TYPE_CHECKING:
    from .review_workflow import AnyWorkflowSnapshot, ReviewWorkflowSnapshot

GUIDANCE_MAX_CHARS = 4000


def _snapshot_types() -> tuple[type, ...]:
    """Review snapshots build on the gate values below, so envelopes import them lazily."""
    from .review_workflow import ReviewWorkflowSnapshot
    return (WorkflowSnapshot, ReviewWorkflowSnapshot)


def _text(value: str, name: str, *, limit: int = 512, empty: bool = False) -> None:
    validate_workflow_text(value, name, limit=limit, empty=empty)


def _count(value: int, name: str, *, minimum: int = 0) -> None:
    validate_workflow_integer(value, name, minimum=minimum)


def _time(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _items(value: tuple, name: str, types: tuple[type, ...], limit: int) -> None:
    if not isinstance(value, tuple) or len(value) > limit or any(
        not isinstance(item, types) for item in value
    ):
        raise ValueError(f"{name} must be a bounded tuple of validated values")


def _command_ids(value: tuple[CommandId, ...], name: str) -> None:
    _items(value, name, (str,), MAX_REARM_COMMANDS)
    for item in value:
        _text(item, name)
    if len(set(value)) != len(value):
        raise ValueError(f"{name} contains duplicate command identities")


@dataclass(frozen=True)
class ResolveSession:
    source: WorkflowAttemptIdentity
    session_id: str
    runtime: str
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, WorkflowAttemptIdentity):
            raise ValueError("resolve session requires its admitted source attempt")
        for name in ("session_id", "runtime", "model"):
            _text(getattr(self, name), name)


@dataclass(frozen=True)
class FreshResolve:
    session: ResolveSession | None = None
    last_applied_admission_version: int = 0

    def __post_init__(self) -> None:
        _count(self.last_applied_admission_version, "continuation admission version")
        if self.session is not None and (
            not isinstance(self.session, ResolveSession) or not self.last_applied_admission_version
        ):
            raise ValueError("resolve session requires an applied admission")


@dataclass(frozen=True)
class AuthoredResolve:
    author_admission: ResolveAdmission
    last_applied_admission_version: int
    session: ResolveSession | None = None
    split: SplitContinuation | None = None
    split_applied_admission_version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.author_admission, ResolveAdmission) or self.author_admission.stage is not ResolveStage.AUTHOR:
            raise ValueError("authored resolve requires its admitted author provenance")
        if self.author_admission.input_fingerprint is None:
            raise ValueError("authored checkpoint requires its admitted input fingerprint")
        _count(self.last_applied_admission_version, "continuation admission version", minimum=1)
        if self.author_admission.revision.version > self.last_applied_admission_version:
            raise ValueError("authored checkpoint cannot exceed the applied admission watermark")
        if self.session is not None and not isinstance(self.session, ResolveSession):
            raise ValueError("authored resolve session must be validated")
        if self.split is not None and not isinstance(self.split, (SplitImplement, NeedsAdjudication)):
            raise ValueError("split continuation must belong to an applied admission")
        _count(self.split_applied_admission_version, "split admission watermark")
        if self.split is not None and self.split.admission_version > self.split_applied_admission_version:
            raise ValueError("split cursor cannot exceed its applied admission watermark")

    @property
    def author_attempt(self) -> WorkflowAttemptIdentity:
        return self.author_admission.attempt


ResolveContinuation = FreshResolve | AuthoredResolve


class ResolveStage(str, Enum):
    ROUTE = "ROUTE"
    RESOLVE = "RESOLVE"
    AUTHOR = "AUTHOR"
    IMPLEMENT = "IMPLEMENT"
    ADJUDICATE = "ADJUDICATE"
    RESUME = "RESUME"


@dataclass(frozen=True)
class ResolveAdmission:
    attempt: WorkflowAttemptIdentity
    revision: WorkflowRevision
    stage: ResolveStage
    adjudication_ceiling: int | None = None
    input_fingerprint: str | None = None
    split_fingerprint: str | None = None
    workspace_handoff_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, WorkflowAttemptIdentity) or not isinstance(self.revision, WorkflowRevision):
            raise ValueError("resolve admission requires captured attempt and revision")
        if self.revision.fence != self.attempt.binding.fence or not self.revision.version:
            raise ValueError("resolve admission requires its newly allocated workflow revision")
        if not isinstance(self.stage, ResolveStage):
            raise ValueError("resolve admission requires a supported stage")
        if self.workspace_handoff_id is not None:
            _text(self.workspace_handoff_id, "workspace handoff")
        if self.stage is ResolveStage.ADJUDICATE:
            if self.adjudication_ceiling is None:
                raise ValueError("adjudication admission requires its configured ceiling")
            _count(self.adjudication_ceiling, "adjudication ceiling")
        elif self.adjudication_ceiling is not None:
            raise ValueError("only adjudication admits against a contract-round ceiling")
        for fingerprint in (self.input_fingerprint, self.split_fingerprint):
            if fingerprint is not None and (
                not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in fingerprint)
            ):
                raise ValueError("admission input fingerprint must be a SHA-256 digest")
        if self.split_fingerprint is not None and self.stage not in (ResolveStage.AUTHOR, ResolveStage.IMPLEMENT, ResolveStage.ADJUDICATE):
            raise ValueError("only split stages carry split input provenance")


def adjudication_allowed(admitted: int, ceiling: int) -> bool:
    _count(admitted, "admitted adjudications")
    _count(ceiling, "adjudication ceiling")
    return admitted < ceiling


@dataclass(frozen=True)
class ResolveEpisode:
    episode_id: str
    failures: int = 0
    last_failure: str = ""
    guidance: str = ""
    continuation: ResolveContinuation = FreshResolve()

    def __post_init__(self) -> None:
        _text(self.episode_id, "episode")
        _count(self.failures, "failures")
        _text(self.last_failure, "last failure", limit=MAX_DETAIL_CHARS, empty=True)
        _text(self.guidance, "guidance", limit=GUIDANCE_MAX_CHARS, empty=True)
        if not isinstance(self.continuation, (FreshResolve, AuthoredResolve)):
            raise ValueError("resolve episode requires a validated continuation")
        sources = (
            (self.continuation.author_attempt,) if isinstance(self.continuation, AuthoredResolve) else ()
        ) + ((self.continuation.session.source,) if self.continuation.session is not None else ()) + (
            split_sources(self.continuation.split) if isinstance(self.continuation, AuthoredResolve) and self.continuation.split is not None else ()
        )
        if any(source.binding.episode_id != self.episode_id for source in sources):
            raise ValueError("resolve continuation cannot inherit another episode")


@dataclass(frozen=True)
class ResolveCeiling:
    episode_id: str
    failures: int
    ceiling: int
    last_failure: str = ""

    def __post_init__(self) -> None:
        _text(self.episode_id, "episode")
        _count(self.failures, "failures")
        _count(self.ceiling, "ceiling", minimum=1)
        if self.failures < self.ceiling:
            raise ValueError("a resolve ceiling cause requires exhaustion")
        _text(self.last_failure, "last failure", limit=MAX_DETAIL_CHARS, empty=True)


@dataclass(frozen=True)
class RemoteOutcomeUncertain:
    command_id: CommandId
    unresolved_since: datetime
    failed_reads: int
    last_error: str

    def __post_init__(self) -> None:
        _text(self.command_id, "command")
        _time(self.unresolved_since, "unresolved since")
        _count(self.failed_reads, "failed reads")
        _text(self.last_error, "last error", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class RetryAuthorizationRequired:
    command_id: CommandId
    absence_evidence_id: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "retry command")
        validate_workflow_identifier(self.absence_evidence_id, "absence evidence")


@dataclass(frozen=True)
class EvidenceUnavailable:
    required_fact: str
    unresolved_since: datetime
    failed_reads: int
    last_error: str

    def __post_init__(self) -> None:
        _text(self.required_fact, "required fact")
        _time(self.unresolved_since, "unresolved since")
        _count(self.failed_reads, "failed reads")
        _text(self.last_error, "last error", limit=MAX_DETAIL_CHARS)


class StopReason(str, Enum):
    TARGET_TERMINAL = "target_terminal"
    DECISION_CHANGED = "decision_changed"
    REQUIRED_EFFECT_REJECTED = "required_effect_rejected"
    OPERATOR_CANCELLED = "operator_cancelled"
    # Review slice: reviews counted outside the authorization reached the
    # rejection ceiling again before the re-arm completed.
    CEILING_REACHED_AGAIN = "ceiling_reached_again"


@dataclass(frozen=True)
class RearmNeedsDecision:
    stopped_request_id: str
    obligations: tuple[CommandId, ...]
    reason: StopReason

    def __post_init__(self) -> None:
        _text(self.stopped_request_id, "stopped request")
        _command_ids(self.obligations, "obligations")
        if not isinstance(self.reason, StopReason):
            raise ValueError("rearm stop reason must be typed")


GateCause = (ResolveCeiling | ReviewRejectionCeiling | ReviewExecutionCeiling | ReviewVerdictHold | RemoteOutcomeUncertain | RetryAuthorizationRequired
             | EvidenceUnavailable | RearmNeedsDecision | ReviewProjectionRefused)
_CAUSES = (ResolveCeiling, ReviewRejectionCeiling, ReviewExecutionCeiling, ReviewVerdictHold, RemoteOutcomeUncertain, RetryAuthorizationRequired, EvidenceUnavailable,
           RearmNeedsDecision, ReviewProjectionRefused)


class RemediationOperation(str, Enum):
    RECONCILE_REMOTE = "reconcile_remote"


class RemediationOutcome(str, Enum):
    UNKNOWN = "unknown"
    VERIFIED_ABSENT = "verified_absent"
    REFUSED = "refused"


@dataclass(frozen=True)
class NoAuthorizedRemediation:
    reason: str

    def __post_init__(self) -> None:
        _text(self.reason, "no-remediation reason", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class RemediationAttempted:
    operation: RemediationOperation
    evidence_ids: tuple[str, ...]
    outcome: RemediationOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.operation, RemediationOperation) or not isinstance(
            self.outcome, RemediationOutcome
        ):
            raise ValueError("remediation requires typed operation and outcome")
        _command_ids(self.evidence_ids, "remediation evidence")
        if not self.evidence_ids:
            raise ValueError("attempted remediation requires durable evidence")


Remediation = NoAuthorizedRemediation | RemediationAttempted


@dataclass(frozen=True)
class OpenGate:
    gate_id: str
    opened_generation: int
    cause: GateCause
    remediation: Remediation

    def __post_init__(self) -> None:
        _text(self.gate_id, "gate")
        _count(self.opened_generation, "gate generation")
        if not isinstance(self.cause, _CAUSES):
            raise ValueError("gate cause must be a supported variant")
        if not isinstance(self.remediation, (NoAuthorizedRemediation, RemediationAttempted)):
            raise ValueError("gate requires explicit remediation provenance")


@dataclass(frozen=True)
class RearmingGate:
    gate: OpenGate
    request_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.gate, OpenGate):
            raise ValueError("rearming requires a validated open gate")
        _text(self.request_id, "request")


UnresolvedGate = OpenGate | RearmingGate


@dataclass(frozen=True)
class KeepGuidance:
    pass


@dataclass(frozen=True)
class ClearGuidance:
    pass


@dataclass(frozen=True)
class SetGuidance:
    text: str

    def __post_init__(self) -> None:
        _text(self.text, "guidance", limit=GUIDANCE_MAX_CHARS)


GuidanceEdit = KeepGuidance | ClearGuidance | SetGuidance


@dataclass(frozen=True)
class OperatorActor:
    """Minted by authenticated transport, never parsed from request JSON."""
    subject: str
    channel: str

    def __post_init__(self) -> None:
        _text(self.subject, "operator")
        _text(self.channel, "operator channel")


@dataclass(frozen=True)
class RequestRearm:
    request_id: str
    expected: WorkflowRevision
    gate_id: str
    guidance: GuidanceEdit
    expected_binding: WorkflowBinding | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        _text(self.request_id, "request")
        _text(self.gate_id, "gate")
        if not isinstance(self.expected, WorkflowRevision):
            raise ValueError("rearm requires a captured workflow revision")
        if not isinstance(self.guidance, (KeepGuidance, ClearGuidance, SetGuidance)):
            raise ValueError("guidance edit must be typed")
        _operator_binding(self.expected, self.expected_binding)


@dataclass(frozen=True)
class RequestReviewExecutionRenewal(RequestRearm):
    execution_epoch: int
    execution_episode: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if target_family(self.expected.fence.key.target) is not WorkflowFamily.REVIEW_REJECTIONS:
            raise ValueError("execution renewal requires a review target")
        if self.expected_binding is None:
            raise ValueError("execution renewal requires its captured rejection binding")
        _count(self.execution_epoch, "execution epoch", minimum=1)
        _text(self.execution_episode, "execution episode")


@dataclass(frozen=True)
class RequestReviewVerdictRelease(RequestReviewExecutionRenewal):
    submission_command_id: CommandId

    def __post_init__(self) -> None:
        super().__post_init__()
        _text(self.submission_command_id, "selected verdict command")


@dataclass(frozen=True)
class ContinueGrantedRearm:
    request_id: str
    expected: WorkflowRevision
    stopped_request_id: str
    expected_binding: WorkflowBinding | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        _text(self.request_id, "request")
        _text(self.stopped_request_id, "stopped request")
        if self.request_id == self.stopped_request_id:
            raise ValueError("continuation requires a new intent identity")
        if not isinstance(self.expected, WorkflowRevision):
            raise ValueError("continuation requires a captured revision")
        _operator_binding(self.expected, self.expected_binding)


@dataclass(frozen=True)
class CancelRearm:
    cancellation_id: str
    expected: WorkflowRevision
    request_id: str
    reason: str
    expected_binding: WorkflowBinding | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        _text(self.cancellation_id, "cancellation")
        _text(self.request_id, "request")
        _text(self.reason, "cancellation reason", limit=MAX_DETAIL_CHARS)
        if not isinstance(self.expected, WorkflowRevision):
            raise ValueError("cancellation requires a captured revision")
        _operator_binding(self.expected, self.expected_binding)


@dataclass(frozen=True)
class ReconcileGate:
    expected: WorkflowRevision
    gate_id: str

    def __post_init__(self) -> None:
        _text(self.gate_id, "gate")
        if not isinstance(self.expected, WorkflowRevision):
            raise ValueError("reconciliation requires a captured revision")


@dataclass(frozen=True)
class AuthorizeAbsentRetry:
    authorization_id: str
    expected: WorkflowRevision
    command_id: CommandId
    gate_id: str
    evidence_id: str
    expected_binding: WorkflowBinding | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        for name in ("authorization_id", "command_id", "gate_id", "evidence_id"):
            _text(getattr(self, name), name)
        if not isinstance(self.expected, WorkflowRevision):
            raise ValueError("retry authorization requires a captured revision")
        _operator_binding(self.expected, self.expected_binding)


def _operator_binding(expected: WorkflowRevision, binding: WorkflowBinding | None) -> None:
    # Older resolve commands have no binding. Review acceptance requires one;
    # retaining it on the command makes exact replay include epoch and episode.
    if binding is not None and (not isinstance(binding, WorkflowBinding) or binding.fence != expected.fence):
        raise ValueError("operator binding must match the captured revision")


OperatorCommand = RequestRearm | ContinueGrantedRearm | CancelRearm | AuthorizeAbsentRetry


@dataclass(frozen=True)
class RenewResolveAllowance:
    episode_id: str

    def __post_init__(self) -> None:
        _text(self.episode_id, "renewed episode")


@dataclass(frozen=True)
class EnsureLifecycleProjection:
    target: WorkflowTarget

    def __post_init__(self) -> None:
        if not isinstance(self.target, IssueRef):
            raise ValueError("resolve projection requires an issue target")


RearmOperation = RenewResolveAllowance | PrepareResolveWorkspace | EnsureLifecycleProjection


@dataclass(frozen=True)
class PendingRearm:
    request_id: str
    gate_id: str
    from_generation: int
    to_generation: int
    operations: tuple[RearmOperation, ...]
    required_commands: tuple[CommandId, ...]

    def __post_init__(self) -> None:
        _text(self.request_id, "request")
        _text(self.gate_id, "gate")
        _count(self.from_generation, "prior generation")
        _count(self.to_generation, "rearm generation", minimum=1)
        if self.to_generation != self.from_generation + 1:
            raise ValueError("a rearm advances generation exactly once")
        _items(self.operations, "operations", (RenewResolveAllowance, PrepareResolveWorkspace, EnsureLifecycleProjection), 3)
        if sum(isinstance(item, EnsureLifecycleProjection) for item in self.operations) != 1:
            raise ValueError("resolve rearm requires exactly one lifecycle projection obligation")
        if sum(isinstance(item, PrepareResolveWorkspace) for item in self.operations) != 1:
            raise ValueError("resolve rearm requires exactly one workspace preparation obligation")
        renewals = tuple(item for item in self.operations if isinstance(item, RenewResolveAllowance))
        if len(renewals) > 1:
            raise ValueError("resolve rearm cannot grant multiple allowances")
        handoff = self.handoff
        if renewals:
            if (handoff.grant_request_id != self.request_id
                    or handoff.source.fence.generation != self.from_generation
                    or handoff.destination.fence.generation != self.to_generation
                    or renewals[0].episode_id != handoff.destination.episode_id):
                raise ValueError("allowance renewal must own its exact workspace handoff")
        elif handoff.destination.fence.generation >= self.to_generation or handoff.grant_request_id == self.request_id:
            raise ValueError("continued rearm must retain an earlier granted workspace handoff")
        if any(isinstance(item, EnsureLifecycleProjection) and item.target != handoff.destination.fence.key.target for item in self.operations):
            raise ValueError("projection and workspace obligations must belong to one target")
        _command_ids(self.required_commands, "required commands")
        if not self.required_commands:
            raise ValueError("pending rearm requires durable command obligations")

    @property
    def handoff(self) -> EpisodeWorkspaceHandoff:
        return next(item.handoff for item in self.operations if isinstance(item, PrepareResolveWorkspace))


@dataclass(frozen=True)
class CompletedRearm:
    request_id: str
    gate_id: str
    to_generation: int
    verified_commands: tuple[CommandId, ...]
    workspace: EpisodeWorkspacePrepared

    def __post_init__(self) -> None:
        _text(self.request_id, "request")
        _text(self.gate_id, "gate")
        _count(self.to_generation, "rearm generation", minimum=1)
        _command_ids(self.verified_commands, "verified commands")
        if not isinstance(self.workspace, EpisodeWorkspacePrepared) or self.workspace.performed_under.fence.generation > self.to_generation:
            raise ValueError("completed rearm requires a nonfuture verified workspace")

    @property
    def handoff(self) -> EpisodeWorkspaceHandoff:
        return self.workspace.handoff


@dataclass(frozen=True)
class SupersededRearm:
    request_id: str
    gate_id: str
    to_generation: int
    reason: StopReason
    evidence_id: str
    verified_commands: tuple[CommandId, ...]
    retired_obligations: tuple[CommandId, ...]
    handoff: EpisodeWorkspaceHandoff

    def __post_init__(self) -> None:
        _text(self.request_id, "request")
        _text(self.gate_id, "gate")
        _text(self.evidence_id, "supersession evidence")
        _count(self.to_generation, "rearm generation", minimum=1)
        if not isinstance(self.reason, StopReason) or self.reason is StopReason.OPERATOR_CANCELLED:
            raise ValueError("supersession requires an observed stop reason")
        _command_ids(self.verified_commands, "verified commands")
        _command_ids(self.retired_obligations, "retired obligations")
        if set(self.verified_commands) & set(self.retired_obligations):
            raise ValueError("a verified obligation cannot also be retired")
        if not isinstance(self.handoff, EpisodeWorkspaceHandoff) or self.handoff.destination.fence.generation > self.to_generation:
            raise ValueError("superseded rearm requires its original workspace handoff")


@dataclass(frozen=True)
class CancelledRearm:
    request_id: str
    gate_id: str
    to_generation: int
    cancellation_id: str
    reason: str
    verified_commands: tuple[CommandId, ...]
    retired_obligations: tuple[CommandId, ...]
    handoff: EpisodeWorkspaceHandoff

    def __post_init__(self) -> None:
        _text(self.request_id, "request")
        _text(self.gate_id, "gate")
        _text(self.cancellation_id, "cancellation")
        _text(self.reason, "cancellation reason", limit=MAX_DETAIL_CHARS)
        _count(self.to_generation, "rearm generation", minimum=1)
        _command_ids(self.verified_commands, "verified commands")
        _command_ids(self.retired_obligations, "retired obligations")
        if set(self.verified_commands) & set(self.retired_obligations):
            raise ValueError("a verified obligation cannot also be retired")
        if not isinstance(self.handoff, EpisodeWorkspaceHandoff) or self.handoff.destination.fence.generation > self.to_generation:
            raise ValueError("cancelled rearm requires its original workspace handoff")


RearmIntent = PendingRearm | CompletedRearm | SupersededRearm | CancelledRearm


@dataclass(frozen=True)
class WorkflowSnapshot:
    revision: WorkflowRevision
    authority: FamilyAuthority
    episode: ResolveEpisode
    gates: tuple[UnresolvedGate, ...] = ()
    intent: RearmIntent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.revision, WorkflowRevision) or not isinstance(
            self.authority, FamilyAuthority
        ) or not isinstance(self.episode, ResolveEpisode):
            raise ValueError("snapshot requires validated revision, authority and episode")
        if self.authority.family is not WorkflowFamily.RESOLVE:
            raise ValueError("this snapshot supports only resolve")
        if not isinstance(self.revision.fence.key.target, IssueRef):
            raise ValueError("resolve workflow requires an issue")
        continuation = self.episode.continuation
        if continuation.last_applied_admission_version > self.revision.version:
            raise ValueError("continuation cannot originate in a future admission")
        if isinstance(continuation, AuthoredResolve) and continuation.split_applied_admission_version > self.revision.version:
            raise ValueError("split cursor cannot originate in a future admission")
        sources = (
            (continuation.author_attempt,) if isinstance(continuation, AuthoredResolve) else ()
        ) + ((continuation.session.source,) if continuation.session is not None else ()) + (
            split_sources(continuation.split) if isinstance(continuation, AuthoredResolve) and continuation.split is not None else ()
        )
        if any(
            source.binding.fence.key != self.revision.fence.key
            or source.binding.authority != self.authority
            or source.binding.fence.generation > self.revision.fence.generation
            for source in sources
        ):
            raise ValueError("continuation provenance disagrees with its workflow authority")
        _items(self.gates, "gates", (OpenGate, RearmingGate), MAX_WORKFLOW_GATES)
        opened = tuple(g.gate if isinstance(g, RearmingGate) else g for g in self.gates)
        if len({g.gate_id for g in opened}) != len(opened):
            raise ValueError("gate identities must be unique")
        if any(g.opened_generation > self.revision.fence.generation for g in opened):
            raise ValueError("a gate cannot originate in a future generation")
        if any(isinstance(g.cause, (ReviewRejectionCeiling, ReviewExecutionCeiling, ReviewVerdictHold, ReviewProjectionRefused)) for g in opened):
            raise ValueError("a resolve workflow cannot hold a review rejection cause")
        if self.intent is not None and not isinstance(
            self.intent, (PendingRearm, CompletedRearm, SupersededRearm, CancelledRearm)
        ):
            raise ValueError("intent must be a validated variant")
        rearming = tuple(g for g in self.gates if isinstance(g, RearmingGate))
        if isinstance(self.intent, PendingRearm):
            if self.intent.to_generation != self.revision.fence.generation:
                raise ValueError("pending intent must own the current generation")
            if len(rearming) != 1 or (
                rearming[0].request_id != self.intent.request_id
                or rearming[0].gate.gate_id != self.intent.gate_id
            ):
                raise ValueError("pending intent must own exactly one rearming gate")
            for operation in self.intent.operations:
                if isinstance(operation, EnsureLifecycleProjection) and operation.target != self.revision.fence.key.target:
                    raise ValueError("projection obligation must belong to this workflow")
                if isinstance(operation, RenewResolveAllowance) and operation.episode_id != self.episode.episode_id:
                    raise ValueError("renewal must name the current episode")
        elif rearming:
            raise ValueError("rearming gate requires a pending owning intent")
        if self.intent is not None and self.intent.to_generation > self.revision.fence.generation:
            raise ValueError("intent cannot belong to a future generation")
        if self.intent is not None:
            destination = self.intent.handoff.destination
            if (destination.fence.key != self.revision.fence.key or destination.authority != self.authority
                    or destination.episode_id != self.episode.episode_id
                    or destination.fence.generation > self.intent.to_generation):
                raise ValueError("rearm workspace handoff must belong to the current episode")

    @property
    def binding(self) -> WorkflowBinding:
        return WorkflowBinding(self.revision.fence, self.authority, self.episode.episode_id)


@dataclass(frozen=True)
class WorkflowAbsent:
    key: WorkflowKey

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey):
            raise ValueError("absence requires a queried workflow identity")


@dataclass(frozen=True)
class WorkflowUnavailable:
    key: WorkflowKey
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey):
            raise ValueError("unavailability requires a queried workflow identity")
        _text(self.reason, "unavailability reason", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class WorkflowMigrationRequired:
    """The target's family has no activation in this repository; never file fallback."""

    key: WorkflowKey
    family: WorkflowFamily = WorkflowFamily.RESOLVE

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey):
            raise ValueError("migration-required state requires a queried workflow identity")
        if not isinstance(self.family, WorkflowFamily) or target_family(self.key.target) is not self.family:
            raise ValueError("migration-required family must own the queried target")

    @property
    def reason(self) -> str:
        return f"{self.family.value}_migration_required"


WorkflowRead = WorkflowSnapshot | WorkflowAbsent | WorkflowMigrationRequired | WorkflowUnavailable


@dataclass(frozen=True)
class ObservedIssue:
    key: WorkflowKey
    issue: IssueView
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey) or not isinstance(self.key.target, IssueRef):
            raise ValueError("issue observation requires an issue identity")
        if not isinstance(self.issue, IssueView) or self.issue.number != self.key.target.number:
            raise ValueError("issue observation identity disagrees with the queried target")
        _count(self.issue.number, "observed issue number", minimum=1)
        if any(not isinstance(getattr(self.issue, field), str) for field in ("title", "body", "updated_at", "author", "state", "state_reason")):
            raise ValueError("observed issue text fields must be strings")
        if not isinstance(self.issue.labels, frozenset) or any(not isinstance(label, Label) for label in self.issue.labels):
            raise ValueError("observed issue labels must be validated label values")
        if type(self.issue.blocked_by_open) is not bool or not isinstance(self.issue.open_blocker_numbers, tuple):
            raise ValueError("observed issue dependencies must be validated")
        for number in self.issue.open_blocker_numbers:
            _count(number, "observed dependency", minimum=1)
        if not isinstance(self.issue.comments, tuple) or any(not isinstance(comment, Comment) or any(not isinstance(getattr(comment, field), str) for field in ("author", "body", "created_at")) for comment in self.issue.comments):
            raise ValueError("observed issue comments must be validated")
        if any(not isinstance(paths, frozenset) or any(not isinstance(path, str) for path in paths) for paths in (self.issue.declared_paths, self.issue.predicted_paths)):
            raise ValueError("observed issue path hints must be string sets")
        _time(self.observed_at, "observation time")
        _text(self.issue.updated_at, "observed issue revision")


@dataclass(frozen=True)
class IssueAbsent:
    key: WorkflowKey
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey) or not isinstance(self.key.target, IssueRef):
            raise ValueError("issue absence requires a queried issue")
        _time(self.observed_at, "observation time")


@dataclass(frozen=True)
class IssueUnavailable:
    key: WorkflowKey
    observed_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkflowKey) or not isinstance(self.key.target, IssueRef):
            raise ValueError("issue unavailability requires a queried issue")
        _time(self.observed_at, "observation time")
        _text(self.reason, "issue observation failure", limit=MAX_DETAIL_CHARS)


IssueObservation = ObservedIssue | IssueAbsent | IssueUnavailable


@dataclass(frozen=True)
class NoLinkedPr:
    issue: WorkflowKey
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.issue, WorkflowKey) or not isinstance(self.issue.target, IssueRef):
            raise ValueError("linkage absence requires a queried issue")
        _time(self.observed_at, "linkage observation time")


@dataclass(frozen=True)
class LinkedPrPresent:
    issue: WorkflowKey
    pr: int
    head_sha: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.issue, WorkflowKey) or not isinstance(self.issue.target, IssueRef):
            raise ValueError("linkage requires a queried issue")
        _count(self.pr, "linked PR", minimum=1)
        _text(self.head_sha, "linked PR head")
        _time(self.observed_at, "linkage observation time")


@dataclass(frozen=True)
class LinkedPrUnavailable:
    issue: WorkflowKey
    observed_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.issue, WorkflowKey) or not isinstance(self.issue.target, IssueRef):
            raise ValueError("linkage failure requires a queried issue")
        _time(self.observed_at, "linkage observation time")
        _text(self.reason, "linkage failure", limit=MAX_DETAIL_CHARS)


LinkedPrObservation = NoLinkedPr | LinkedPrPresent | LinkedPrUnavailable


@dataclass(frozen=True)
class ResolveRearmPreflight:
    """Harness observations captured under the deployment effect barriers."""
    issue: ObservedIssue
    linkage: NoLinkedPr
    external_holds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.issue, ObservedIssue) or not isinstance(self.linkage, NoLinkedPr) or self.issue.key != self.linkage.issue:
            raise ValueError("resolve rearm preflight requires matching complete observations")
        _items(self.external_holds, "external holds", (str,), MAX_WORKFLOW_GATES)
        for hold in self.external_holds:
            _text(hold, "external hold", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class ResolveRearmAcceptance:
    preflight: ResolveRearmPreflight
    workspace: ResolveWorkspaceObserved

    def __post_init__(self) -> None:
        if (not isinstance(self.preflight, ResolveRearmPreflight)
                or not isinstance(self.workspace, ResolveWorkspaceObserved)
                or self.workspace.source.fence.key != self.preflight.issue.key):
            raise ValueError("rearm acceptance requires matching remote and workspace observations")

    @property
    def issue(self) -> ObservedIssue:
        return self.preflight.issue

    @property
    def linkage(self) -> NoLinkedPr:
        return self.preflight.linkage

    @property
    def external_holds(self) -> tuple[str, ...]:
        return self.preflight.external_holds


@dataclass(frozen=True)
class CommandAbsenceEvidence:
    """Current postcondition is absent; this does not prove historical non-execution."""
    evidence_id: str
    command_id: CommandId
    binding: WorkflowBinding
    observed_at: datetime
    remote_revision: str

    def __post_init__(self) -> None:
        for name in ("evidence_id", "command_id", "remote_revision"):
            _text(getattr(self, name), name)
        if not isinstance(self.binding, WorkflowBinding):
            raise ValueError("absence evidence requires a captured workflow binding")
        _time(self.observed_at, "absence observation time")


@dataclass(frozen=True)
class CommandRetryApplicability:
    """Fresh harness evaluation that the original decision still permits execution."""
    evidence_id: str
    command_id: CommandId
    binding: WorkflowBinding
    observed_at: datetime
    decision_guard_digest: str

    def __post_init__(self) -> None:
        for name in ("evidence_id", "command_id", "decision_guard_digest"):
            _text(getattr(self, name), name)
        if not isinstance(self.binding, WorkflowBinding):
            raise ValueError("retry applicability requires a captured binding")
        _time(self.observed_at, "applicability observation time")


@dataclass(frozen=True)
class StandaloneCommand:
    pass


@dataclass(frozen=True)
class RearmObligation:
    request_id: str

    def __post_init__(self) -> None:
        _text(self.request_id, "rearm obligation request")


CommandAssociation = StandaloneCommand | RearmObligation


@dataclass(frozen=True)
class UnconsumedRetryAuthorization:
    authorization_id: str
    command_id: CommandId
    gate_id: str
    evidence_id: str
    binding: WorkflowBinding
    association: CommandAssociation

    def __post_init__(self) -> None:
        for name in ("authorization_id", "command_id", "gate_id", "evidence_id"):
            _text(getattr(self, name), name)
        if not isinstance(self.binding, WorkflowBinding):
            raise ValueError("retry authorization requires a captured binding")
        if not isinstance(self.association, (StandaloneCommand, RearmObligation)):
            raise ValueError("retry authorization requires a typed command association")


@dataclass(frozen=True)
class ConsumedRetryAuthorization:
    authorization: UnconsumedRetryAuthorization
    command_attempt: int

    def __post_init__(self) -> None:
        if not isinstance(self.authorization, UnconsumedRetryAuthorization):
            raise ValueError("consumption requires a validated authorization")
        _count(self.command_attempt, "command attempt", minimum=1)


@dataclass(frozen=True)
class RetryBegan:
    consumed: ConsumedRetryAuthorization

    def __post_init__(self) -> None:
        if not isinstance(self.consumed, ConsumedRetryAuthorization):
            raise ValueError("new retry requires committed consumption")


@dataclass(frozen=True)
class RetryAlreadyConsumed:
    consumed: ConsumedRetryAuthorization

    def __post_init__(self) -> None:
        if not isinstance(self.consumed, ConsumedRetryAuthorization):
            raise ValueError("retry replay requires prior consumption")


class RetryDisposition(str, Enum):
    """How an operator grant ended without buying an attempt."""
    NOT_NEEDED = "not_needed"
    INAPPLICABLE = "inapplicable"
    VOID = "void"


@dataclass(frozen=True)
class ClosedRetryAuthorization:
    """A terminal grant that bought no attempt; it can never match again.

    Every grant ends at the executor's first spend decision (consumed or one of
    these), so a deterministic gate identity cannot revive an old grant.
    """
    authorization: UnconsumedRetryAuthorization
    disposition: RetryDisposition
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.authorization, UnconsumedRetryAuthorization):
            raise ValueError("a closed grant requires a validated authorization")
        if not isinstance(self.disposition, RetryDisposition):
            raise ValueError("a closed grant requires a typed disposition")
        _text(self.reason, "grant disposition reason", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class RetryDeclined:
    closed: ClosedRetryAuthorization

    def __post_init__(self) -> None:
        if not isinstance(self.closed, ClosedRetryAuthorization):
            raise ValueError("a declined retry requires its terminal grant")


@dataclass(frozen=True)
class RetryStale:
    """The command's reconciliation moved after the executor's read; nothing was spent."""


@dataclass(frozen=True)
class ReconciliationPolicy:
    initial_delay_seconds: int = 30
    maximum_delay_seconds: int = 1800
    maximum_age_seconds: int = 3600
    maximum_reads: int = 20
    # Proven-absent re-executions of one unresolved command before an operator
    # grant is required; unknown outcomes charge no effect refusals.
    maximum_reexecutions: int = 4

    def __post_init__(self) -> None:
        for name in ("initial_delay_seconds", "maximum_delay_seconds", "maximum_age_seconds", "maximum_reads",
                     "maximum_reexecutions"):
            _count(getattr(self, name), name, minimum=1)
        if not self.initial_delay_seconds <= self.maximum_delay_seconds <= 86400:
            raise ValueError("reconciliation delay must be bounded to one day")
        if self.maximum_age_seconds > 30 * 86400 or self.maximum_reads > 10000 or self.maximum_reexecutions > 100:
            raise ValueError("reconciliation age/read/re-execution ceiling is excessive")


@dataclass(frozen=True)
class ActiveReconciliation:
    unresolved_since: datetime
    next_read_at: datetime
    failed_reads: int
    # Proven-absent re-executions spent under this history (ADR 0015 backoff).
    reexecutions: int = 0

    def __post_init__(self) -> None:
        _time(self.unresolved_since, "unresolved since")
        _time(self.next_read_at, "next read")
        _count(self.failed_reads, "failed reads")
        _count(self.reexecutions, "re-executions")
        if self.next_read_at < self.unresolved_since:
            raise ValueError("next reconciliation cannot precede unresolved history")


@dataclass(frozen=True)
class ReconciliationOnly:
    history: ActiveReconciliation
    gate_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.history, ActiveReconciliation):
            raise ValueError("reconciliation-only mode retains unresolved history")
        _text(self.gate_id, "uncertainty gate")


@dataclass(frozen=True)
class RetiredUncertainCommand:
    uncertainty: ReconciliationOnly
    retirement_evidence_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.uncertainty, ReconciliationOnly):
            raise ValueError("retired uncertainty remains read-only reconcilable")
        _text(self.retirement_evidence_id, "retirement evidence")


@dataclass(frozen=True)
class RetiredSettledCommand:
    retirement_evidence_id: str
    outcome_evidence_id: str

    def __post_init__(self) -> None:
        _text(self.retirement_evidence_id, "retirement evidence")
        _text(self.outcome_evidence_id, "current outcome evidence")


CommandReconciliation = ActiveReconciliation | ReconciliationOnly | RetiredUncertainCommand | RetiredSettledCommand


class ResolveResultKind(str, Enum):
    COUNTED_REQUEUE = "counted_requeue"
    INFRA_FAILURE = "infra_failure"
    BUDGET_REFUSAL = "budget_refusal"
    BLOCKED = "blocked"
    PR_OPENED = "pr_opened"
    STAGE_PROGRESSED = "stage_progressed"


@dataclass(frozen=True)
class ResolveAttemptResult:
    attempt: WorkflowAttemptIdentity
    kind: ResolveResultKind
    summary: str = ""
    stage: ResolveStage = ResolveStage.RESOLVE
    session: ResolveSession | None = None
    split: SplitContinuation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, WorkflowAttemptIdentity) or not isinstance(self.kind, ResolveResultKind):
            raise ValueError("result requires captured binding and typed classification")
        _text(self.summary, "result summary", limit=MAX_DETAIL_CHARS, empty=True)
        if not isinstance(self.stage, ResolveStage):
            raise ValueError("resolve result requires a supported stage")
        if self.session is not None and (
            not isinstance(self.session, ResolveSession) or self.session.source != self.attempt
            or self.stage is ResolveStage.ROUTE
        ):
            raise ValueError("resolve session must belong to this non-router attempt")
        if self.split is not None and (
            not isinstance(self.split, (SplitImplement, NeedsAdjudication))
            or self.split.source != self.attempt
            or self.stage not in (ResolveStage.AUTHOR, ResolveStage.IMPLEMENT, ResolveStage.ADJUDICATE)
            or self.kind in (ResolveResultKind.BUDGET_REFUSAL, ResolveResultKind.PR_OPENED)
            or (self.kind is ResolveResultKind.INFRA_FAILURE and not (
                (self.stage is ResolveStage.ADJUDICATE and isinstance(self.split, NeedsAdjudication))
                or (self.stage is ResolveStage.IMPLEMENT and isinstance(self.split, SplitImplement))
            ))
        ):
            raise ValueError("split progression requires this executed split attempt")


class WorkflowMutation(str, Enum):
    OPEN_GATE = "open_gate"
    RECORD_RESULT = "record_result"
    REQUEST_REARM = "request_rearm"
    CONTINUE_REARM = "continue_rearm"
    RECONCILE_REARM = "reconcile_rearm"
    STOP_REARM = "stop_rearm"
    AUTHORIZE_RETRY = "authorize_retry"
    RECORD_REVIEW = "record_review"
    # Review-only: a fresh complete read disproved the rejection ceiling. It
    # advances the generation so no in-flight older review can re-cover the
    # head the ceiling held (ADR 0015).
    RELEASE_CEILING = "release_ceiling"


@dataclass(frozen=True)
class AuthorizedOperatorCommand:
    actor: OperatorActor
    command: OperatorCommand
    preflight: ResolveRearmPreflight | ResolveRearmAcceptance | ReviewRearmPreflight | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.actor, OperatorActor) or not isinstance(
            self.command, (RequestRearm, ContinueGrantedRearm, CancelRearm, AuthorizeAbsentRetry)
        ):
            raise ValueError("operator transition requires authenticated actor and typed command")
        if isinstance(self.command, (RequestRearm, ContinueGrantedRearm)) and target_family(
            self.command.expected.fence.key.target
        ) is WorkflowFamily.REVIEW_REJECTIONS:
            if not isinstance(self.preflight, ReviewRearmPreflight) or self.preflight.pr_key != self.command.expected.fence.key:
                raise ValueError("review rearm authorization requires this pull request's complete review preflight")
        elif isinstance(self.command, RequestRearm):
            if (not isinstance(self.preflight, ResolveRearmAcceptance)
                    or self.preflight.workspace.source.fence != self.command.expected.fence):
                raise ValueError("new allowance requires this workflow's observed workspace acceptance")
        elif isinstance(self.command, ContinueGrantedRearm):
            if not isinstance(self.preflight, ResolveRearmPreflight) or self.preflight.issue.key != self.command.expected.fence.key:
                raise ValueError("rearm authorization requires this workflow's harness preflight")
        elif self.preflight is not None:
            raise ValueError("this operator command does not accept a rearm preflight")


@dataclass(frozen=True)
class ObservedTransition:
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _command_ids(self.evidence_ids, "transition evidence")
        if not self.evidence_ids:
            raise ValueError("observed transition requires durable evidence")


TransitionAudit = AuthorizedOperatorCommand | ResolveAttemptResult | ObservedTransition


@dataclass(frozen=True)
class WorkflowChanged:
    expected: WorkflowRevision
    mutation: WorkflowMutation
    snapshot: AnyWorkflowSnapshot
    audit: TransitionAudit
    commands: tuple[LifecycleCommand, ...] = ()
    # Review transitions carry the exact snapshot they replace so provenance
    # (continuation, authorization chain) is checked at construction. The
    # commit adapter proves it equal to the stored row; it is never persisted.
    prior: "ReviewWorkflowSnapshot | None" = None

    def __post_init__(self) -> None:
        from .review_workflow import (
            BoundReviewSubmission, CancelledReviewRearm, CompletedReviewRearm, PendingReviewRearm,
            ReviewedBaseline, ReviewWorkflowSnapshot, SupersededReviewRearm, validate_review_change,
        )
        if not isinstance(self.expected, WorkflowRevision) or not isinstance(
            self.snapshot, (WorkflowSnapshot, ReviewWorkflowSnapshot)
        ) or not isinstance(self.mutation, WorkflowMutation):
            raise ValueError("transition requires typed revision, mutation and snapshot")
        review = isinstance(self.snapshot, ReviewWorkflowSnapshot)
        before, after = self.expected, self.snapshot.revision
        if before.fence.key != after.fence.key or after.version != before.version + 1:
            raise ValueError("a transition advances one version of the same workflow")
        advances = self.mutation in (
            WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM, WorkflowMutation.RELEASE_CEILING,
        )
        if after.fence.generation != before.fence.generation + int(advances):
            raise ValueError("only accepted rearm intents and ceiling releases advance generation")
        if not isinstance(self.audit, (AuthorizedOperatorCommand, ResolveAttemptResult, ObservedTransition)):
            raise ValueError("transition requires typed audit provenance")
        operator_variants = {
            WorkflowMutation.REQUEST_REARM: RequestRearm,
            WorkflowMutation.CONTINUE_REARM: ContinueGrantedRearm,
            WorkflowMutation.AUTHORIZE_RETRY: AuthorizeAbsentRetry,
        }
        required_command = operator_variants.get(self.mutation)
        if required_command is not None:
            if not isinstance(self.audit, AuthorizedOperatorCommand) or not isinstance(self.audit.command, required_command) or self.audit.command.expected != self.expected:
                raise ValueError("operator transition requires matching authorized request")
        elif self.mutation is WorkflowMutation.RECORD_RESULT:
            if review:
                raise ValueError("attempt results belong to the resolve family")
            if not isinstance(self.audit, ResolveAttemptResult) or self.audit.attempt.binding != self.snapshot.binding:
                raise ValueError("result transition requires this episode's trusted attempt identity")
        elif self.mutation is WorkflowMutation.RECORD_REVIEW:
            if not review:
                raise ValueError("review completion belongs to the review rejection family")
            continuation = self.snapshot.episode.continuation
            if (not isinstance(continuation, ReviewedBaseline)
                    or not isinstance(continuation.source, BoundReviewSubmission)
                    or continuation.reviewed_generation != self.snapshot.revision.fence.generation):
                raise ValueError("recorded review must establish a bound baseline of the current generation")
            if not isinstance(self.audit, ObservedTransition) or str(continuation.source.command_id) not in self.audit.evidence_ids:
                raise ValueError("recorded review requires its bound submission evidence")
        elif self.mutation is WorkflowMutation.RELEASE_CEILING:
            if not review:
                raise ValueError("ceiling release belongs to the review rejection family")
            if not isinstance(self.audit, ObservedTransition):
                raise ValueError("ceiling release requires observed evidence")
        elif self.mutation is WorkflowMutation.STOP_REARM:
            terminal = (SupersededReviewRearm, CancelledReviewRearm) if review else (SupersededRearm, CancelledRearm)
            if not isinstance(self.snapshot.intent, terminal):
                raise ValueError("stopped rearm requires a terminal intent")
            if isinstance(self.snapshot.intent, (CancelledRearm, CancelledReviewRearm)):
                if not isinstance(self.audit, AuthorizedOperatorCommand) or not isinstance(self.audit.command, CancelRearm) or self.audit.command.expected != self.expected or self.audit.command.request_id != self.snapshot.intent.request_id or self.audit.command.cancellation_id != self.snapshot.intent.cancellation_id:
                    raise ValueError("cancelled rearm requires the matching operator cancellation")
            elif not isinstance(self.audit, ObservedTransition):
                raise ValueError("superseded rearm requires observed evidence")
        elif not isinstance(self.audit, ObservedTransition):
            raise ValueError("observed transition requires observed audit provenance")
        if advances and self.mutation is not WorkflowMutation.RELEASE_CEILING:
            intent = self.snapshot.intent
            pending = PendingReviewRearm if review else PendingRearm
            if not isinstance(intent, pending) or not isinstance(self.audit, AuthorizedOperatorCommand) or not isinstance(self.audit.command, (RequestRearm, ContinueGrantedRearm)) or intent.request_id != self.audit.command.request_id:
                raise ValueError("accepted rearm must own a matching pending intent")
            if isinstance(self.audit.command, RequestRearm) and intent.gate_id != self.audit.command.gate_id:
                raise ValueError("accepted rearm must own its selected gate")
        completed = CompletedReviewRearm if review else CompletedRearm
        if self.mutation is WorkflowMutation.RECONCILE_REARM and not isinstance(self.snapshot.intent, completed):
            raise ValueError("completed rearm requires a completed intent")
        _items(self.commands, "commands", (LifecycleCommand,), MAX_REARM_COMMANDS)
        if len({c.command_id for c in self.commands}) != len(self.commands):
            raise ValueError("transition command identities must be unique")
        if any(c.repository != before.fence.key.repository for c in self.commands):
            raise ValueError("transition cannot cross repository authority")
        if any(c.workflow != self.snapshot.binding for c in self.commands):
            raise ValueError("transition commands require the resulting workflow binding")
        if review:
            validate_review_change(self)
        elif self.prior is not None:
            raise ValueError("resolve transitions do not carry a review prior")


@dataclass(frozen=True)
class WorkflowUnchanged:
    snapshot: AnyWorkflowSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, _snapshot_types()):
            raise ValueError("unchanged transition requires a validated snapshot")


class WorkflowRejection(str, Enum):
    STALE_REVISION = "stale_revision"
    NOT_EXHAUSTED = "not_exhausted"
    REARM_PENDING = "rearm_pending"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    OTHER_HOLD = "other_hold"
    INVALID_SCOPE = "invalid_scope"
    MISSING_AUTHORIZATION = "missing_authorization"
    AUTHORIZATION_OUTSTANDING = "authorization_outstanding"
    # An observed counted set larger than one re-arm's dismissal bound: the gate
    # stays visible and the operator is told why it cannot be re-armed as is.
    AUTHORIZATION_BOUND_EXCEEDED = "authorization_bound_exceeded"


@dataclass(frozen=True)
class WorkflowRejected:
    code: WorkflowRejection
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, WorkflowRejection):
            raise ValueError("workflow rejection must be typed")
        _text(self.reason, "workflow rejection", limit=MAX_DETAIL_CHARS)


WorkflowTransition = WorkflowChanged | WorkflowUnchanged | WorkflowRejected


@dataclass(frozen=True)
class WorkflowCommitted:
    snapshot: AnyWorkflowSnapshot
    idempotent: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, _snapshot_types()) or type(self.idempotent) is not bool:
            raise ValueError("workflow commit requires a snapshot and boolean replay marker")


@dataclass(frozen=True)
class WorkflowConflict:
    current: WorkflowRevision
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.current, WorkflowRevision):
            raise ValueError("workflow conflict requires the current revision")
        _text(self.reason, "workflow conflict", limit=MAX_DETAIL_CHARS)


@dataclass(frozen=True)
class HeldProjection:
    snapshot: AnyWorkflowSnapshot
    external_holds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, _snapshot_types()):
            raise ValueError("held projection requires a validated snapshot")
        _items(self.external_holds, "external holds", (str,), MAX_WORKFLOW_GATES)
        for reason in self.external_holds:
            _text(reason, "external hold", limit=MAX_DETAIL_CHARS)

    @property
    def held(self) -> bool:
        return bool(self.snapshot.gates or self.external_holds)
