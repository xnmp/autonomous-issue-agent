"""Bounded, closed tagged encoding for workflow authority; ADR 0015.

Only registered value constructors can be reconstituted. Their runtime checks
apply equally to database reads and normal construction; persisted JSON cannot
choose an import or silently populate a newly added optional field.
"""

from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
import json
from typing import TypeVar

from ..domain import workflow as w
from ..domain import review_workflow as rw
from ..domain import review_dispatch_record as rd
from ..domain import review_workflow_policy as rp
from ..domain import review_execution as rx
from ..domain import review_rounds as rr
from ..domain import review_round_archive as round_archive
from ..domain import review_round_receipt_archive as receipt_archive
from ..domain import review_checkpoint_chain as checkpoint_chain
from ..domain import review_checkpoint_archive as checkpoint_archive
from ..domain import review_archive_source_versions as archive_source_versions
from ..domain import review_lifecycle_archive_audit as lifecycle_archive_audit
from ..domain import review_registration_membership_audit as registration_membership_audit
from ..domain import review_initial_checkpoint_absence as initial_checkpoint_absence
from ..domain import review_checkpoint_stream_replay as checkpoint_replay
from ..domain import review_checkpoint_application as checkpoint_application
from ..domain import review_execution_archive_gate as archive_gate
from ..domain import review_archive_identity as archive_identity
from ..domain import review_preflight as preflight
from ..domain import review_return as review_return
from ..domain.agent_task import RuntimeName
from ..domain.review import ReviewMode
from ..domain import workflow_identity as identity
from ..domain.contracts import WorkflowAttemptIdentity
from ..domain.labels import Label
from ..domain.world import Comment, IssueView
from ..domain.outcome import ExitReason
from ..domain.outcome import Adjudication
from ..domain.effort import Effort
from ..domain import resolve_split as split
from ..domain import resolve_workspace as workspace
from ..domain.resolve_rearm import RearmObservation
from ..domain.resolve_return import ResolveInputs, ReturnedResolve, PendingResolveReturn, FinalizedResolveReturn, RetiredResolveReturn, ResolveCleanupCompleted

MAX_WORKFLOW_PAYLOAD_BYTES = 262144
MAX_WORKFLOW_PAYLOAD_DEPTH = 24
T = TypeVar("T")

_VALUES = (
    archive_identity.ScanningReviewArchiveIdentities,
    archive_identity.CompletedReviewArchiveIdentities,
    archive_identity.BlockedReviewArchiveIdentities,
    archive_identity.ReviewArchiveIdentityCursor,
    RearmObservation,
    workspace.ResolveWorkspaceObserved, w.ResolveRearmAcceptance,
    workspace.ObservedResolveBase, workspace.NoPriorResolveAdmission,
    workspace.PriorResolveAdmission, workspace.EpisodeWorkspaceHandoff,
    workspace.PrepareResolveWorkspace, workspace.EpisodeWorkspacePrepared,
    split.ContractRevision, split.ContractBaseline, split.UnavailableContractRevision,
    split.SplitSelection, split.SplitReply, split.AdjudicationStep, split.SplitContext,
    split.SplitImplement, split.NeedsAdjudication, split.ContractResetIntent,
    split.ContractResetResult, split.ContractResetCompleted, split.AdjudicationInput,
    split.ReauthorResetIntent, split.ReauthorInput,
    ResolveInputs, ReturnedResolve, PendingResolveReturn, FinalizedResolveReturn, RetiredResolveReturn, ResolveCleanupCompleted,
    identity.IssueRef, identity.PullRequestRef, identity.WorkflowKey,
    identity.WorkflowFence, identity.FamilyAuthority, identity.WorkflowRevision,
    identity.WorkflowBinding, WorkflowAttemptIdentity,
    Comment, IssueView, w.ObservedIssue, w.NoLinkedPr, w.LinkedPrPresent, w.ResolveRearmPreflight,
    w.ResolveSession, w.FreshResolve, w.AuthoredResolve, w.ResolveAdmission,
    w.ResolveEpisode, w.ResolveCeiling, w.RemoteOutcomeUncertain, w.RetryAuthorizationRequired,
    w.EvidenceUnavailable, w.RearmNeedsDecision, w.NoAuthorizedRemediation,
    w.RemediationAttempted, w.OpenGate, w.RearmingGate,
    w.KeepGuidance, w.ClearGuidance, w.SetGuidance, w.OperatorActor,
    w.RequestRearm, w.ContinueGrantedRearm, w.CancelRearm, w.ReconcileGate,
    w.AuthorizeAbsentRetry, w.RenewResolveAllowance, w.EnsureLifecycleProjection,
    w.PendingRearm, w.CompletedRearm, w.SupersededRearm, w.CancelledRearm,
    w.WorkflowSnapshot, w.CommandAbsenceEvidence, w.CommandRetryApplicability,
    w.StandaloneCommand, w.RearmObligation, w.UnconsumedRetryAuthorization,
    w.ConsumedRetryAuthorization, w.ClosedRetryAuthorization, w.ActiveReconciliation, w.ReconciliationOnly,
    w.RetiredUncertainCommand, w.RetiredSettledCommand, w.ResolveAttemptResult,
    w.AuthorizedOperatorCommand, w.ObservedTransition, w.WorkflowRejected,
    # Review-rejection family (ADR 0015 review slice).
    rw.CountedReview, rw.CountingRule, rw.ReviewRejectionCeiling, rw.ReviewRearmPreflight,
    rw.FreshReviewRequired, rw.BoundReviewSubmission, rw.ImportedBaseline, rw.ReviewedBaseline,
    rw.ReviewEpisode, rw.DismissCountedReview, rw.RequireFreshReview, rw.EnsureReviewProjection, rw.ReviewProjectionPlan,
    rw.PendingReviewRearm, rw.CompletedReviewRearm, rw.SupersededReviewRearm, rw.CancelledReviewRearm,
    rw.ReviewWorkflowSnapshot, rw.ReviewFamilyEpisodes, rw.SubmittedReviewFact, rw.CompleteReviews, rw.PartialReviews,
    rw.ObservedPullRequest, rw.PullRequestAbsent, rw.PullRequestUnavailable,
    rw.ReviewProjectionRefused, rw.RecordedPullRequest,
    # The review daemon's recorded dispatch decision (operator surface).
    rp.ReviewHeld, rp.ReviewOwed, rp.ReviewNotOwed, rp.ReleaseProjectionPending, rp.SubmissionUnresolved,
    rp.CeilingEscalation, rp.CeilingProductivityDecision,
    rd.ReviewNotAdmitted, rd.ReviewDeferred, rd.ReviewTerminal, rd.ReviewWithheld, rd.ReviewInFlight,
    rd.DecisionBasis, rd.DepartedReadFailure, rd.RecordedReviewDecision,
    rx.ReviewExecutionBinding, rx.ReviewRoundIdentity, rx.ReviewChildIdentity,
    rx.ReviewChildAdmission, rx.ReviewUnlaunchedRelease, rx.ReviewChildAdmissionRefusal, rx.ReviewChildLaunchRegistration,
    review_return.ReviewReturnedEnvelope, review_return.ReturnedReviewChild,
    review_return.ReviewRuntimePreflightStop,
    rx.LaunchedReviewChild, rx.ImportedReviewSession, rx.NoReviewSession,
    rx.ReusableReviewSession, rx.PreserveReviewSession, rx.ReplaceReviewSession,
    rx.ClearReviewSession, rx.PreserveDisplayedRound, rx.SetDisplayedRound,
    rx.PaidReviewProgress, rx.CopiedReviewProgress, rx.AddReviewStrike,
    rx.SaturateReviewFailures, rx.ReviewRoundFailure, rx.ReviewInfrastructureDeath, rx.ReviewBudgetExhausted, rx.ReviewPinDeferred,
    rx.ReviewAdmissionRefused, rx.ReviewReservationReleased, rx.ReviewUnlaunchedRound,
    rx.ReviewPreparationFailure, rx.ReviewPreparationDeferred,
    rx.ReviewRoundCancelled, rx.ReviewRoundOutcome, rx.ReviewExecutionPolicy,
    rx.ReviewExecutionState, rx.ReviewExecutionBoundaryCursor,
    rx.ReviewGenerationArchiveFingerprint, rx.ReviewGenerationReceiptSweepCursor,
    rx.ReviewGenerationArchiveReceiptMember, rx.ReviewGenerationArchiveBaseline,
    rx.ReviewGenerationArchiveScan,
    rx.CompletedReviewGenerationArchiveScan, rx.ReviewGenerationArchiveSweep,
    rx.CompletedReviewGenerationArchiveAudit,
    rx.BlockedReviewGenerationArchiveScan, rx.BlockedReviewGenerationArchiveSweep,
    rx.ReviewGenerationArchiveCursor,
    round_archive.ReviewRoundArchiveFingerprint,
    round_archive.ReviewRoundArchiveBaseline,
    round_archive.ReviewRoundArchiveScan,
    round_archive.CompletedReviewRoundArchiveScan,
    round_archive.BlockedReviewRoundArchiveScan,
    round_archive.ReviewRoundArchiveCursor,
    receipt_archive.ReviewRoundReceiptSweepCursor,
    receipt_archive.ReviewRoundReceiptArchiveMember,
    receipt_archive.ReviewRoundReceiptArchiveSource,
    receipt_archive.ReviewRoundReceiptArchiveBaseline,
    receipt_archive.ReviewRoundReceiptArchiveScan,
    receipt_archive.CompletedReviewRoundReceiptArchiveScan,
    receipt_archive.ReviewRoundReceiptArchiveSweep,
    receipt_archive.CompletedReviewRoundReceiptArchiveAudit,
    receipt_archive.BlockedReviewRoundReceiptArchiveScan,
    receipt_archive.BlockedReviewRoundReceiptArchiveSweep,
    receipt_archive.ReviewRoundReceiptArchiveCursor,
    checkpoint_chain.ReviewCheckpointChainCursor,
    checkpoint_chain.ObservedReviewCheckpointChainSource,
    checkpoint_chain.ObservedReviewCheckpointChainPage,
    checkpoint_archive.ReviewCheckpointArchiveBaseline,
    archive_source_versions.ReviewArchiveSourceVersions,
    lifecycle_archive_audit.ReviewLifecycleMembership,
    lifecycle_archive_audit.ScanningReviewLifecycleCommands,
    lifecycle_archive_audit.ScanningReviewLifecycleEffects,
    lifecycle_archive_audit.CompletedReviewLifecycleArchiveAudit,
    lifecycle_archive_audit.BlockedReviewLifecycleArchiveAudit,
    registration_membership_audit.ScanningReviewRegistrationMembership,
    registration_membership_audit.CompletedReviewRegistrationMembership,
    registration_membership_audit.BlockedReviewRegistrationMembership,
    initial_checkpoint_absence.ScanningInitialCheckpointAbsence,
    initial_checkpoint_absence.CompletedInitialCheckpointAbsence,
    initial_checkpoint_absence.BlockedInitialCheckpointAbsence,
    checkpoint_archive.ReviewCheckpointArchiveCursor,
    checkpoint_archive.ReviewCheckpointArchiveAwaiting,
    checkpoint_archive.ReviewCheckpointArchiveSegment,
    checkpoint_archive.ReviewCheckpointArchiveReplaySegment,
    checkpoint_archive.ReviewCheckpointArchivePendingBoundary,
    checkpoint_archive.ReviewCheckpointArchiveStorageAnchor,
    checkpoint_replay._CheckpointReplayBetweenBoundaries,
    checkpoint_replay._CheckpointReplayWithinBoundary,
    checkpoint_replay._CheckpointReplayTail,
    checkpoint_archive.CompletedReviewCheckpointArchiveAudit,
    checkpoint_archive.BlockedReviewCheckpointArchiveAudit,
    checkpoint_application.ApplyAllCheckpointTailOutcomes,
    checkpoint_application.ApplyCheckpointTailByFollowingBoundary,
    checkpoint_application.ReviewCheckpointApplicationReceipt,
    checkpoint_application.ReviewCheckpointApplicationProposal,
    archive_gate.CertifiedReviewExecutionArchive,
    archive_gate.BlockedReviewExecutionArchive,
    rx.ReviewExecutionCheckpoint, rx.ReviewExecutionCompactionRoundSource,
    rx.ReviewExecutionCheckpointReceipt,
    rx.ReviewExecutionGenerationBoundary, rx.ReviewExecutionAcceptedTransitionSource,
    rx.ReviewExecutionTransitionProofSource,
    rx.ReviewExecutionOrigin, rx.ImportedReviewExecutionOriginReceipt,
    rx.InitializedReviewExecutionOriginReceipt,
    rx.ImportedReviewExecutionDecision,
    rx.ExecutionNotActivated, rx.ActivatedReviewExecution, rx.CurrentReviewExecution,
    rr.ReviewStagePlan, rr.ReviewSubmissionPlan, rr.ReviewSubmissionRegistration, rr.ReviewRoundPlan,
    rr.ReviewChildPending, rr.ReviewChildRefused, rr.ReviewChildReleased,
    rr.ReviewChildSettledWithoutLaunch, rr.ReviewChildNotAdmitted,
    rr.ReviewChildPinDeferred, rr.ReviewChildSucceeded, rr.OrdinaryReviewFailure,
    rr.DeterministicReviewFailure, rr.ReviewChildFailed, rr.ReviewChildCancelled,
    rr.ReviewChildInfrastructureFailure, rr.ReviewChildBudgetExhausted,
    rr.ReviewChildReturned, rr.ReviewSubmissionPending,
    rr.ReviewSubmissionConfirmed, rr.ReviewSubmissionRefused,
    rr.ReviewSubmissionEvidence, rr.ReviewPlanningOpen,
    rr.ReviewPlanningComplete, rr.ReviewPlanningRefused, rr.ReviewPlanningSealed,
    rr.ReviewParentAbandoned, rr.ReviewSealedParentAbandoned,
    rr.ReviewRoundFacts, rr.ReviewRoundAwaitingEvidence,
    rr.CurrentReviewOutcomeReceipt,
    rr.StaleReviewProductivitySettlement,
    rr.FinalizedReviewRound, rr.DeferredReviewPreparation,
    preflight.ReviewPreflightSelection,
)
_ENUMS = (
    ExitReason, Adjudication, Effort,
    Label, identity.WorkflowFamily, w.WorkflowMutation, w.StopReason, w.WorkflowRejection,
    w.RetryDisposition, w.RemediationOperation,
    w.RemediationOutcome, w.ResolveResultKind, w.ResolveStage,
    rw.SubmittedReviewState, rw.PullRequestState, rp.ReviewKind, rd.DecisionRead, rd.WithheldReason,
    RuntimeName, ReviewMode, rx.ReviewChildKind, rx.ReviewPreparationKind,
    rx.ReviewCancellationReason, rx.ReviewExecutionOriginKind,
    rr.ReviewSubmissionKind, rr.ReviewSessionReuse, rr.ReviewRoundWait,
)
_TAGS = {value.__name__: value for value in (*_VALUES, *_ENUMS)}
if len(_TAGS) != len(_VALUES) + len(_ENUMS):
    raise RuntimeError("workflow codec tags must name exactly one registered variant")

_CURRENT_REVIEW_SNAPSHOT_TAG = "ReviewWorkflowSnapshotV2"
_LEGACY_REVIEW_SNAPSHOT_FIELDS = frozenset({"revision", "authority", "episode", "gates", "intent"})
_CURRENT_ACTIVATED_EXECUTION_TAG = "ActivatedReviewExecutionV2"
_CURRENT_EXECUTION_AUTHORITY_TAG = "CurrentReviewExecutionV1"
_LEGACY_ACTIVATED_EXECUTION_FIELDS = frozenset({
    "origin", "checkpoint", "state", "last_sequence", "checkpoint_receipt",
})
_CURRENT_EXECUTION_CHECKPOINT_TAG = "ReviewExecutionCheckpointV2"
_LEGACY_EXECUTION_CHECKPOINT_FIELDS = frozenset({"through_sequence", "state", "policy"})

# Explicit evolution of already-persisted variants. Never infer defaults from
# dataclass declarations: new authority fields require a deliberate read rule.
_LEGACY_FIELDS = {
    "ReviewExecutionCompactionRoundSource": {"lineage_sha256": None},
    # Histories persisted before re-executions were counted had spent none.
    "ActiveReconciliation": {"reexecutions": 0},
    "AuthoredResolve": {"split": None, "split_applied_admission_version": 0},
    "ResolveAdmission": {"split_fingerprint": None, "workspace_handoff_id": None},
    "ResolveAttemptResult": {"split": None},
    "ReturnedResolve": {"split_input": None, "verdict": "", "rationale": "", "contract_revision": None},
    # Review facts observed before command markers were parsed name no command.
    "SubmittedReviewFact": {"review_command": None},
    "RequestRearm": {"expected_binding": None},
    "ContinueGrantedRearm": {"expected_binding": None},
    "CancelRearm": {"expected_binding": None},
    "AuthorizeAbsentRetry": {"expected_binding": None},
}


class WorkflowEncodingError(ValueError):
    pass


def _encode(value: object, depth: int = 0) -> object:
    if depth > MAX_WORKFLOW_PAYLOAD_DEPTH:
        raise WorkflowEncodingError("workflow payload nesting is excessive")
    if type(value) in _ENUMS:
        return {"type": type(value).__name__, "value": value.value}
    if type(value) in _VALUES and is_dataclass(value):
        return {
            "type": (_CURRENT_REVIEW_SNAPSHOT_TAG if type(value) is rw.ReviewWorkflowSnapshot
                     else _CURRENT_ACTIVATED_EXECUTION_TAG if type(value) is rx.ActivatedReviewExecution
                     else _CURRENT_EXECUTION_AUTHORITY_TAG if type(value) is rx.CurrentReviewExecution
                     else _CURRENT_EXECUTION_CHECKPOINT_TAG if type(value) is rx.ReviewExecutionCheckpoint
                     else type(value).__name__),
            "fields": {
                field.name: _encode(getattr(value, field.name), depth + 1)
                for field in fields(value)
                # Resolve's accepted wire format stays byte-stable. Review
                # commands retain their required binding; legacy audits omit it.
                if (field.name not in {"expected_binding", "lineage_sha256"}
                    or getattr(value, field.name) is not None)
            },
        }
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkflowEncodingError("workflow timestamps require an offset")
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, tuple):
        return [_encode(item, depth + 1) for item in value]
    if isinstance(value, frozenset):
        encoded = [_encode(item, depth + 1) for item in value]
        return {"type": "frozenset", "items": sorted(encoded, key=lambda item: json.dumps(item, sort_keys=True))}
    if value is None or type(value) in (str, int, bool):
        return value
    raise WorkflowEncodingError(f"unsupported workflow value: {type(value).__name__}")


def _decode(value: object, depth: int = 0) -> object:
    if depth > MAX_WORKFLOW_PAYLOAD_DEPTH:
        raise WorkflowEncodingError("workflow payload nesting is excessive")
    if isinstance(value, list):
        return tuple(_decode(item, depth + 1) for item in value)
    if value is None or type(value) in (str, int, bool):
        return value
    if not isinstance(value, dict):
        raise WorkflowEncodingError("unsupported workflow JSON value")
    tag = value.get("type")
    if not isinstance(tag, str):
        raise WorkflowEncodingError("workflow value requires a tagged variant")
    if tag == "datetime":
        if set(value) != {"type", "value"} or not isinstance(value["value"], str):
            raise WorkflowEncodingError("malformed workflow timestamp")
        result = datetime.fromisoformat(value["value"])
        if result.tzinfo is None or result.utcoffset() is None:
            raise WorkflowEncodingError("workflow timestamps require an offset")
        return result
    if tag == "frozenset":
        if set(value) != {"type", "items"} or not isinstance(value["items"], list):
            raise WorkflowEncodingError("malformed workflow set")
        items = tuple(_decode(item, depth + 1) for item in value["items"])
        result = frozenset(items)
        if len(result) != len(items):
            raise WorkflowEncodingError("duplicate workflow set members")
        return result
    if tag == "ReviewWorkflowSnapshot":
        payload = value.get("fields")
        if set(value) != {"type", "fields"} or not isinstance(payload, dict) or set(payload) != _LEGACY_REVIEW_SNAPSHOT_FIELDS:
            raise WorkflowEncodingError("wrong fields for legacy review snapshot")
        decoded = {key: _decode(item, depth + 1) for key, item in payload.items()}
        return rw.ReviewWorkflowSnapshot(**decoded, execution=rx.ExecutionNotActivated())
    if tag == "ActivatedReviewExecution":
        payload = value.get("fields")
        if (set(value) != {"type", "fields"} or not isinstance(payload, dict)
                or set(payload) != _LEGACY_ACTIVATED_EXECUTION_FIELDS):
            raise WorkflowEncodingError("wrong fields for legacy activated execution")
        decoded = {key: _decode(item, depth + 1) for key, item in payload.items()}
        return rx.ActivatedReviewExecution(**decoded, boundaries=())
    if tag == "ReviewExecutionCheckpoint":
        payload = value.get("fields")
        if (set(value) != {"type", "fields"} or not isinstance(payload, dict)
                or set(payload) != _LEGACY_EXECUTION_CHECKPOINT_FIELDS):
            raise WorkflowEncodingError("wrong fields for legacy execution checkpoint")
        decoded = {key: _decode(item, depth + 1) for key, item in payload.items()}
        return rx.ReviewExecutionCheckpoint(**decoded, boundary_cursor=None)
    if tag == _CURRENT_REVIEW_SNAPSHOT_TAG:
        tag = "ReviewWorkflowSnapshot"
    if tag == _CURRENT_ACTIVATED_EXECUTION_TAG:
        tag = "ActivatedReviewExecution"
    if tag == _CURRENT_EXECUTION_AUTHORITY_TAG:
        tag = "CurrentReviewExecution"
    if tag == _CURRENT_EXECUTION_CHECKPOINT_TAG:
        tag = "ReviewExecutionCheckpoint"
    constructor = _TAGS.get(tag)
    if constructor is None:
        raise WorkflowEncodingError(f"unknown workflow variant: {tag}")
    if issubclass(constructor, Enum):
        if set(value) != {"type", "value"}:
            raise WorkflowEncodingError("malformed workflow enum")
        return constructor(value["value"])
    payload = value.get("fields")
    if set(value) != {"type", "fields"} or not isinstance(payload, dict):
        raise WorkflowEncodingError("malformed workflow fields")
    legacy = _LEGACY_FIELDS.get(tag, {})
    if legacy and set(payload) == {field.name for field in fields(constructor)} - legacy.keys():
        payload = {**legacy, **payload}
    if set(payload) != {field.name for field in fields(constructor)}:
        raise WorkflowEncodingError(f"wrong fields for workflow variant: {tag}")
    return constructor(**{key: _decode(item, depth + 1) for key, item in payload.items()})


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowEncodingError(f"duplicate workflow JSON field: {key}")
        result[key] = value
    return result


def encode_workflow(value: object) -> str:
    try:
        encoded = json.dumps(
            {"format": 1, "value": _encode(value)},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > MAX_WORKFLOW_PAYLOAD_BYTES:
            raise WorkflowEncodingError("workflow payload is too large")
        return encoded
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowEncodingError(str(exc)) from exc


def decode_workflow(raw: str, expected: type[T]) -> T:
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_WORKFLOW_PAYLOAD_BYTES:
            raise WorkflowEncodingError("workflow payload is invalid or too large")
        envelope = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(envelope, dict) or set(envelope) != {"format", "value"} or type(envelope["format"]) is not int or envelope["format"] != 1:
            raise WorkflowEncodingError("unsupported workflow encoding format")
        decoded = _decode(envelope["value"])
        if not isinstance(decoded, expected):
            raise WorkflowEncodingError("workflow payload has the wrong root variant")
        return decoded
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise WorkflowEncodingError(str(exc)) from exc
