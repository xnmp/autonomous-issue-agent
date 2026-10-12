"""Bounded, closed tagged encoding for workflow authority; ADR 0015.

Only registered value constructors can be reconstituted. Their runtime checks
apply equally to database reads and normal construction; persisted JSON cannot
choose an import or silently populate a newly added optional field.
"""

from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from functools import lru_cache
import json
from typing import TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain import workflow as w
from ..domain import review_workflow as rw
from ..domain import review_dispatch_record as rd
from ..domain import review_workflow_policy as rp
from ..domain.review_evidence import EvidenceSchedule
from ..domain.review import PostedReview
from ..domain import review_execution as rx
from ..domain import review_execution_migration as migration
from ..domain.review_execution_import_plan import EligibleZeroHistoryReviewImport
from ..domain.review_execution_history_import import ReviewImportNoHistoryObserved
from ..domain.review_execution_counter_mapping import LegacyOpenPRCounterLink
from ..domain import review_rounds as rr
from ..domain import review_preflight as preflight
from ..domain import review_return as review_return
from ..domain.agent_task import RuntimeName
from ..domain.review import ReviewMode
from ..domain import workflow_identity as identity
from ..domain.contracts import BudgetScope, WorkflowAttemptIdentity, ContinuationAdmission, Money, TokenUsage, UsageSource, PricingBasis, ChargeOrigin, SettlementRequest
from ..domain import continuation_components as cc
from ..domain.continuation_import import ContinuationImportPlan, ContinuationImportTarget
from ..domain.labels import Label
from ..domain.eligibility import IssueCompletionGuard, LifecycleOperation
from ..domain.world import Comment, IssueView
from ..domain.outcome import ExitReason
from ..domain.self_clear import BudgetBlock, MachineBlock, MachineBlocker, MachineClearProjection, BudgetClearProjection, BudgetWorld, BudgetLimitObservation
from ..domain.actions import Target
from ..domain.contracts import EffectReceipt, EffectStatus
from ..domain.outcome import Adjudication
from ..domain.effort import Effort
from ..domain import resolve_split as split
from ..domain import resolve_workspace as workspace
from ..domain.resolve_rearm import RearmObservation
from ..domain.resolve_return import ResolveInputs, ReturnedResolve, PendingResolveReturn, FinalizedResolveReturn, RetiredResolveReturn, ResolveCleanupCompleted

MAX_WORKFLOW_PAYLOAD_BYTES = 262144
MAX_WORKFLOW_PAYLOAD_DEPTH = 24
T = TypeVar("T")


def decode_self_clear_projection(payload: object) -> MachineClearProjection | BudgetClearProjection:
    if not isinstance(payload, str):
        raise ValueError("self-clear projection requires a bounded encoded value")
    value = decode_workflow(payload, object)
    if not isinstance(value, (MachineClearProjection, BudgetClearProjection)):
        raise ValueError("self-clear command requires its fixed recovery projection")
    return value

_VALUES = (
    EvidenceSchedule, EffectReceipt,
    MachineClearProjection, BudgetClearProjection, BudgetWorld, BudgetLimitObservation,
    BudgetScope, BudgetBlock, cc.RefusedBudgetBlock,
    MachineBlock, cc.RecordedMachineBlock,
    cc.ReviewRetryState, cc.IssueReviewPurchase, cc.ReviewRepairResult,
    cc.IssueRebasePurchase, cc.RebaseRepairResult, cc.RebaseAllowanceReset,
    cc.IssueCIPurchase, cc.CIRepairResult, cc.CIEpisodeReset,
    IssueCompletionGuard, Money, TokenUsage, SettlementRequest, cc.RepairModel, cc.PRRepairResult, cc.PRRepairNotLaunched, cc.PRRepairUnreturned, cc.RepairHandoff,
    ContinuationImportPlan, ContinuationImportTarget,
    ContinuationAdmission, identity.ContinuationComponentIdentity,
    cc.ContinuationWatermarks, cc.RepairSession, cc.IssueRecoveryState, cc.PRRepairState, cc.PRBlankReplySource,
    cc.CIRemediationCapture, cc.OwedCIRemediation, cc.SettledCIRemediation, cc.ImportedCIRemediation,
    cc.BranchRefreshCapture, cc.PendingCIEscalationCapture, cc.PublicationIdentityEscalationCapture, cc.OwedPRRecoveryEffect,
    cc.SettledPRRecoveryEffect, cc.ImportedBranchRefresh, cc.ImportedPendingCIEscalation,
    cc.FixInfraEpisode, cc.FixInfraReceipt, cc.IssueInfraPurchase,
    cc.StewardLaneState, cc.StewardState, cc.StewardAttemptSource, cc.StewardCommandGuard,
    cc.StewardFailed, cc.StewardVerdict, cc.StewardStopped, cc.GroomContent, cc.GroomNoFinding, cc.ContinuationImportBlocker,
    cc.InactiveIssueRecovery, cc.InactivePRRepair, cc.InactiveSteward,
    cc.BlockedIssueRecovery, cc.BlockedPRRepair, cc.BlockedSteward,
    cc.ActiveIssueRecovery, cc.ActivePRRepair, cc.ActiveSteward,
    w.IssueWorkflowShell, w.IssueFamilyEpisodes,
    cc.ContinuationCapture, cc.IssueRecoveryPurchase, cc.IssueReplyPurchase, cc.PRRepairPurchase, cc.StewardPurchase,
    EligibleZeroHistoryReviewImport, ReviewImportNoHistoryObserved, LegacyOpenPRCounterLink,
    migration.ReviewImportSource, migration.ReviewImportBlocker, migration.ReviewImportHistoryBlocked,
    migration.BlockedReviewExecutionMigration,
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
    w.RequestRearm, w.RequestReviewExecutionRenewal, w.RequestReviewVerdictRelease,
    w.ContinueGrantedRearm, w.CancelRearm, w.ReconcileGate,
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
    rw.ReviewRejectionGrant, rw.ReviewExecutionRenewalGrant, rw.ReviewVerdictReleaseGrant,
    w.ReviewExecutionCeiling, w.ReviewVerdictHold,
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
    rx.LaunchedReviewChild, rx.ImportedReviewSession, rx.RenewedReviewSessionSource, rx.NoReviewSession,
    rx.ReusableReviewSession, rx.PreserveReviewSession, rx.ReplaceReviewSession,
    rx.ClearReviewSession, rx.PreserveDisplayedRound, rx.SetDisplayedRound,
    rx.PaidReviewProgress, rx.CopiedReviewProgress, rx.AddReviewStrike,
    rx.SaturateReviewFailures, rx.ReviewRoundFailure, rx.ReviewInfrastructureDeath, rx.ReviewBudgetExhausted, rx.ReviewPinDeferred,
    rx.ReviewAdmissionRefused, rx.ReviewReservationReleased, rx.ReviewUnlaunchedRound,
    rx.ReviewPreparationFailure, rx.ReviewPreparationDeferred,
    rx.ReviewRoundCancelled, rx.ReviewRoundOutcome, rx.ReviewExecutionPolicy,
    rx.ReviewExecutionState, rx.ReviewExecutionBoundaryCursor,
    rx.ReviewExecutionCheckpoint, rx.ReviewExecutionGenerationBoundary, rx.ReviewExecutionAcceptedTransitionSource,
    rx.ReviewExecutionTransitionProofSource,
    rx.ReviewExecutionOrigin, rx.ImportedReviewExecutionOriginReceipt,
    rx.InitializedReviewExecutionOriginReceipt, rx.RenewedReviewExecutionOriginReceipt,
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
    rr.CurrentStaleReviewProductivitySettlement,
    rr.StaleReviewProductivitySettlement,
    rr.FinalizedReviewRound, rr.DeferredReviewPreparation,
    preflight.ReviewPreflightSelection,
)
_ENUMS = (
    PostedReview,
    MachineBlocker, Target, EffectStatus,
    UsageSource, PricingBasis, ChargeOrigin,
    LifecycleOperation,
    cc.RepairAllowance, cc.CICharge, cc.ReviewCharge, cc.RebaseCharge, cc.StewardLane, cc.StewardFailureKind, cc.FixInfraOutcome,
    migration.ReviewImportBlockerKind,
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

# The closed constructors do not change between payloads. Cache their schema,
# never decoded authority values or validation results.
_VALUE_FIELDS = {value: fields(value) for value in _VALUES}
_FIELD_NAMES = {value: frozenset(field.name for field in schema) for value, schema in _VALUE_FIELDS.items()}
_ENUM_TYPES = frozenset(_ENUMS)
_EVOLVED_TAGS = {"WorkflowSnapshotV2": "WorkflowSnapshot", "ReviewWorkflowSnapshotV3": "ReviewWorkflowSnapshot",
                 "ReviewFamilyEpisodesV2": "ReviewFamilyEpisodes"}

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
_LEGACY_FIELDS: dict[str, dict[str, object]] = {
    "SettlementRequest": {"accounting_gap": "", "origin": {"type": "ChargeOrigin", "value": "normal_return"}},
    "BudgetWorld": {"config_version": ""},
    "MachineClearProjection": {"binding": None},
    "BudgetClearProjection": {"binding": None},
    "BudgetLimitObservation": {"budget_scope": None},
    "StewardState": {"groom_content": {"type": "GroomContent", "fields": {"attempted": "", "successful": ""}}},
    "StewardAttemptSource": {"content_signature": ""},
    "StewardPurchase": {"content_signature": "", "infra_ceiling": None},
    "StewardLaneState": {"delivery_source": None, "attempted_at": ""},
    "WorkflowSnapshot": {
        "issue_recovery": {"type": "InactiveIssueRecovery", "fields": {}},
        "steward": {"type": "InactiveSteward", "fields": {}},
    },
    "ReviewWorkflowSnapshot": {"pr_repair": {"type": "InactivePRRepair", "fields": {}}},
    "ReviewFamilyEpisodes": {"pr_repair": {"type": "InactivePRRepair", "fields": {}}},
    # Histories persisted before re-executions were counted had spent none.
    "ActiveReconciliation": {"reexecutions": 0},
    "PRRepairState": {"resumes": 0, "actual_model": None, "blank_reply": None, "ci_rerun": None, "ci_cancel": None, "branch_refresh": None, "pending_ci_escalation": None},
    "PRRepairPurchase": {"branch": "", "handoff": None, "advance_resume": False, "head": ""},
    "PRRepairResult": {"ci": None, "review": None, "rebase": None, "machine_block": None},
    "IssueRecoveryState": {"pending_ci": None, "pending_rebase": None, "infra_episode": None, "machine_block": None, "budget_block": None, "self_clear_ceiling_reported": False},
    "ContinuationCapture": {"fix_infra_ceiling": None},
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
    "PendingReviewRearm": {"grant": {"type": "ReviewRejectionGrant", "fields": {}}},
    "CompletedReviewRearm": {"grant": {"type": "ReviewRejectionGrant", "fields": {}}},
    "SupersededReviewRearm": {"grant": {"type": "ReviewRejectionGrant", "fields": {}}},
    "CancelledReviewRearm": {"grant": {"type": "ReviewRejectionGrant", "fields": {}}},
}


class WorkflowEncodingError(ValueError):
    pass


def _encode(value: object, depth: int = 0) -> object:
    if depth > MAX_WORKFLOW_PAYLOAD_DEPTH:
        raise WorkflowEncodingError("workflow payload nesting is excessive")
    if type(value) in _ENUM_TYPES:
        return {"type": type(value).__name__, "value": value.value}
    if type(value) in _VALUE_FIELDS and is_dataclass(value):
        return {
            "type": ("ReviewWorkflowSnapshotV3" if type(value) is rw.ReviewWorkflowSnapshot and not isinstance(value.pr_repair, cc.InactivePRRepair)
                     else _CURRENT_REVIEW_SNAPSHOT_TAG if type(value) is rw.ReviewWorkflowSnapshot
                     else "WorkflowSnapshotV2" if type(value) is w.WorkflowSnapshot and (
                         not isinstance(value.issue_recovery, cc.InactiveIssueRecovery) or not isinstance(value.steward, cc.InactiveSteward))
                     else "ReviewFamilyEpisodesV2" if type(value) is rw.ReviewFamilyEpisodes and not isinstance(value.pr_repair, cc.InactivePRRepair)
                     else _CURRENT_ACTIVATED_EXECUTION_TAG if type(value) is rx.ActivatedReviewExecution
                     else _CURRENT_EXECUTION_AUTHORITY_TAG if type(value) is rx.CurrentReviewExecution
                     else _CURRENT_EXECUTION_CHECKPOINT_TAG if type(value) is rx.ReviewExecutionCheckpoint
                     else type(value).__name__),
            "fields": {
                # Only this historical field used a scalar wire format.
                field.name: (value.review_failures.count if type(value) is cc.IssueRecoveryState
                    and field.name == "review_failures" and not value.review_failures.review_at
                    and value.review_failures.pr is None and value.review_failures.pending is None
                    else _encode(getattr(value, field.name), depth + 1))
                for field in _VALUE_FIELDS[type(value)]
                # Resolve's accepted wire format stays byte-stable. Review
                # commands retain their required binding; legacy audits omit it.
                if (not (type(value) is cc.PRRepairState and (field.name == "resumes" and value.resumes == 0 or field.name == "actual_model" and value.actual_model is None or field.name == "blank_reply" and value.blank_reply is None or field.name in {"ci_rerun", "ci_cancel", "branch_refresh", "pending_ci_escalation"} and getattr(value, field.name) is None))
                    and not (type(value) is cc.ContinuationCapture and field.name == "fix_infra_ceiling" and value.fix_infra_ceiling is None)
                    and not (type(value) is cc.IssueRecoveryState and field.name in {"pending_ci", "pending_rebase", "infra_episode", "machine_block", "budget_block"} and getattr(value, field.name) is None)
                    and not (type(value) is cc.IssueRecoveryState and field.name == "self_clear_ceiling_reported" and not value.self_clear_ceiling_reported)
                    and not (type(value) is BudgetWorld and field.name == "config_version" and not value.config_version)
                    and not (type(value) is SettlementRequest and field.name == "accounting_gap" and not value.accounting_gap)
                    and not (type(value) is SettlementRequest and field.name == "origin" and value.origin is ChargeOrigin.NORMAL_RETURN)
                    and not (type(value) is BudgetLimitObservation and field.name == "budget_scope" and value.budget_scope is None)
                    and not (type(value) in {MachineClearProjection, BudgetClearProjection} and field.name == "binding" and value.binding is None)
                    and not (type(value) is cc.PRRepairResult and (field.name == "ci" and value.ci is None
                        or field.name == "review" and value.review is None
                        or field.name == "rebase" and value.rebase is None
                        or field.name == "machine_block" and value.machine_block is None))
                    and not (type(value) is cc.PRRepairPurchase and (field.name == "branch" and not value.branch
                        or field.name == "handoff" and value.handoff is None
                        or field.name == "advance_resume" and not value.advance_resume
                        or field.name == "head" and not value.head))
                    and not (type(value) is cc.StewardLaneState and (
                        field.name == "delivery_source" and value.delivery_source is None
                        or field.name == "attempted_at" and not value.attempted_at))
                    and not (type(value) in {cc.StewardAttemptSource, cc.StewardPurchase}
                             and field.name == "content_signature" and not value.content_signature)
                    and not (type(value) is cc.StewardPurchase and field.name == "infra_ceiling" and value.infra_ceiling is None)
                    and not (type(value) is cc.StewardState and field.name == "groom_content"
                             and value.groom_content == cc.GroomContent())
                    and (field.name not in {"expected_binding", "lineage_sha256"}
                     or getattr(value, field.name) is not None)
                    and not (field.name == "grant" and isinstance(getattr(value, field.name), rw.ReviewRejectionGrant))
                    and not (field.name in {"issue_recovery", "steward", "pr_repair"}
                             and isinstance(getattr(value, field.name), (cc.InactiveIssueRecovery, cc.InactiveSteward, cc.InactivePRRepair))
                             and (type(value) in (rw.ReviewWorkflowSnapshot, rw.ReviewFamilyEpisodes)
                                  or type(value) is w.WorkflowSnapshot and isinstance(value.issue_recovery, cc.InactiveIssueRecovery)
                                  and isinstance(value.steward, cc.InactiveSteward))))
            },
        }
    if isinstance(value, ZoneInfo):
        return {"type": "timezone", "value": value.key}
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
    if tag == "timezone":
        if set(value) != {"type", "value"} or not isinstance(value["value"], str) or not 0 < len(value["value"]) <= 256:
            raise WorkflowEncodingError("malformed workflow timezone")
        try:
            return ZoneInfo(value["value"])
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise WorkflowEncodingError("unknown workflow timezone") from exc
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
    strict_evolution = tag in _EVOLVED_TAGS
    if strict_evolution:
        tag = _EVOLVED_TAGS[tag]
    elif tag == _CURRENT_REVIEW_SNAPSHOT_TAG:
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
    if tag == "MachineClearProjection" and "ignored_checks" in payload:
        if not isinstance(payload["ignored_checks"], str):
            raise WorkflowEncodingError("legacy ignored check policy is malformed")
        # Old commands/receipts requalify through today's policy at the effect boundary.
        payload = {key: item for key, item in payload.items() if key != "ignored_checks"}
    legacy: dict[str, object] = {} if strict_evolution else _LEGACY_FIELDS.get(tag, {})
    field_names = _FIELD_NAMES[constructor]
    required = ({"baseline_at", "stalls", "infra_failures", "pending_delivery"}
                if tag == "StewardLaneState" else field_names - legacy.keys())
    if tag in {"SettlementRequest", "StewardLaneState", "StewardPurchase", "PRRepairState", "PRRepairPurchase", "PRRepairResult", "IssueRecoveryState"} and required <= set(payload) <= field_names:
        payload = {**legacy, **payload}
    if legacy and set(payload) == field_names - legacy.keys():
        payload = {**legacy, **payload}
    if set(payload) != field_names:
        raise WorkflowEncodingError(f"wrong fields for workflow variant: {tag}")
    decoded = {key: _decode(item, depth + 1) for key, item in payload.items()}
    if tag == "IssueRecoveryState" and type(decoded["review_failures"]) is int:
        decoded["review_failures"] = cc.ReviewRetryState(count=decoded["review_failures"])
    return constructor(**decoded)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowEncodingError(f"duplicate workflow JSON field: {key}")
        result[key] = value
    return result


@lru_cache(maxsize=128)
def _parsed_workflow_json(raw: str) -> object:
    # Private trees are read-only inputs to _decode, never returned to callers.
    # The public decoder bounds cached inputs to 16 KiB and constructs typed
    # values afresh so constructor and authority validation cannot be skipped.
    return json.loads(raw, object_pairs_hook=_unique_object)


def validate_operator_json(raw: bytes) -> None:
    """Apply receipt codec bounds before HTTP decoding can exhaust recursion."""
    try:
        if len(raw) > MAX_WORKFLOW_PAYLOAD_BYTES:
            raise ValueError("operator payload is too large")
        stack = [(json.loads(raw, object_pairs_hook=_unique_object), 0)]
        while stack:
            value, depth = stack.pop()
            if depth > MAX_WORKFLOW_PAYLOAD_DEPTH:
                raise ValueError("operator payload nesting is excessive")
            children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
            stack.extend((child, depth + 1) for child in children)
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise WorkflowEncodingError("invalid or excessive operator JSON") from exc


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
        if not isinstance(raw, str):
            raise WorkflowEncodingError("workflow payload is invalid or too large")
        source_bytes = len(raw.encode("utf-8"))
        if source_bytes > MAX_WORKFLOW_PAYLOAD_BYTES:
            raise WorkflowEncodingError("workflow payload is invalid or too large")
        envelope = _parsed_workflow_json(raw) if type(raw) is str and source_bytes <= 16384 \
            else json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(envelope, dict) or set(envelope) != {"format", "value"} or type(envelope["format"]) is not int or envelope["format"] != 1:
            raise WorkflowEncodingError("unsupported workflow encoding format")
        decoded = _decode(envelope["value"])
        if not isinstance(decoded, expected):
            raise WorkflowEncodingError("workflow payload has the wrong root variant")
        return decoded
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise WorkflowEncodingError(str(exc)) from exc
