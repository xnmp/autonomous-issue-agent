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
from ..domain.agent_task import RuntimeName
from ..domain import workflow_identity as identity
from ..domain.contracts import BudgetScope, WorkflowAttemptIdentity, ContinuationAdmission, Money, TokenUsage, UsageSource, PricingBasis, ChargeOrigin, SettlementRequest
from ..domain import continuation_components as cc
from ..domain.labels import Label
from ..domain.eligibility import IssueCompletionGuard, LifecycleOperation
from ..domain.world import Comment, IssueView
from ..domain.outcome import ExitReason
from ..domain.actions import Target
from ..domain.contracts import EffectReceipt, EffectStatus
from ..domain import resolve_workspace as workspace
from ..domain.resolve_rearm import RearmObservation
from ..domain.resolve_return import ResolveInputs, ReturnedResolve, PendingResolveReturn, FinalizedResolveReturn, RetiredResolveReturn, ResolveCleanupCompleted

MAX_WORKFLOW_PAYLOAD_BYTES = 262144
MAX_WORKFLOW_PAYLOAD_DEPTH = 24
T = TypeVar("T")


_VALUES = (
    EffectReceipt,
    BudgetScope,
    cc.ReviewRetryState, cc.IssueReviewPurchase, cc.ReviewRepairResult,
    cc.IssueRebasePurchase, cc.RebaseRepairResult,
    cc.IssueCIPurchase, cc.CIRepairResult, cc.CIEpisodeReset,
    IssueCompletionGuard, Money, TokenUsage, SettlementRequest, cc.RepairModel, cc.PRRepairResult, cc.PRRepairNotLaunched, cc.PRRepairUnreturned, cc.RepairHandoff,
    ContinuationAdmission, identity.ContinuationComponentIdentity,
    cc.ContinuationWatermarks, cc.RepairSession, cc.IssueRecoveryState, cc.PRRepairState, cc.PRBlankReplySource,
    cc.CIRemediationCapture, cc.OwedCIRemediation, cc.SettledCIRemediation,
    cc.BranchRefreshCapture, cc.PendingCIEscalationCapture, cc.PublicationIdentityEscalationCapture, cc.OwedPRRecoveryEffect,
    cc.SettledPRRecoveryEffect,
    cc.FixInfraReceipt, cc.IssueInfraPurchase,
    cc.InactiveIssueRecovery, cc.InactivePRRepair,
    cc.ActiveIssueRecovery, cc.ActivePRRepair,
    w.IssueWorkflowShell, w.IssueFamilyEpisodes,
    cc.ContinuationCapture, cc.IssueRecoveryPurchase, cc.IssueReplyPurchase, cc.PRRepairPurchase,
    RearmObservation,
    workspace.ResolveWorkspaceObserved, w.ResolveRearmAcceptance,
    workspace.ObservedResolveBase, workspace.NoPriorResolveAdmission,
    workspace.PriorResolveAdmission, workspace.EpisodeWorkspaceHandoff,
    workspace.PrepareResolveWorkspace, workspace.EpisodeWorkspacePrepared,
    workspace.ContractRevision, workspace.ContractResetResult,
    ResolveInputs, ReturnedResolve, PendingResolveReturn, FinalizedResolveReturn, RetiredResolveReturn, ResolveCleanupCompleted,
    identity.IssueRef, identity.PullRequestRef, identity.WorkflowKey,
    identity.WorkflowFence, identity.FamilyAuthority, identity.WorkflowRevision,
    identity.WorkflowBinding, WorkflowAttemptIdentity,
    Comment, IssueView, w.ObservedIssue, w.NoLinkedPr, w.LinkedPrPresent, w.ResolveRearmPreflight,
    w.ResolveSession, w.FreshResolve, w.ResolveAdmission,
    w.ResolveEpisode, w.ResolveCeiling, w.RemoteOutcomeUncertain, w.RetryAuthorizationRequired,
    w.EvidenceUnavailable, w.RearmNeedsDecision, w.NoAuthorizedRemediation,
    w.RemediationAttempted, w.OpenGate, w.RearmingGate,
    w.KeepGuidance, w.ClearGuidance, w.SetGuidance, w.OperatorActor,
    w.RequestRearm,
    w.ContinueGrantedRearm, w.CancelRearm, w.ReconcileGate,
    w.AuthorizeAbsentRetry, w.RenewResolveAllowance, w.EnsureLifecycleProjection,
    w.PendingRearm, w.CompletedRearm, w.SupersededRearm, w.CancelledRearm,
    w.WorkflowSnapshot, w.CommandAbsenceEvidence, w.CommandRetryApplicability,
    w.StandaloneCommand, w.RearmObligation, w.UnconsumedRetryAuthorization,
    w.ConsumedRetryAuthorization, w.ClosedRetryAuthorization, w.ActiveReconciliation, w.ReconciliationOnly,
    w.RetiredUncertainCommand, w.RetiredSettledCommand, w.ResolveAttemptResult,
    w.AuthorizedOperatorCommand, w.ObservedTransition, w.WorkflowRejected,
)
_ENUMS = (
    Target, EffectStatus,
    UsageSource, PricingBasis, ChargeOrigin,
    LifecycleOperation,
    cc.RepairAllowance, cc.CICharge, cc.ReviewCharge, cc.RebaseCharge, cc.FixInfraOutcome,
    ExitReason,
    Label, identity.WorkflowFamily, w.WorkflowMutation, w.StopReason, w.WorkflowRejection,
    w.RetryDisposition, w.RemediationOperation,
    w.RemediationOutcome, w.ResolveResultKind, w.ResolveStage,
    RuntimeName,
)
_TAGS = {value.__name__: value for value in (*_VALUES, *_ENUMS)}
if len(_TAGS) != len(_VALUES) + len(_ENUMS):
    raise RuntimeError("workflow codec tags must name exactly one registered variant")

# The closed constructors do not change between payloads. Cache their schema,
# never decoded authority values or validation results.
_VALUE_FIELDS = {value: fields(value) for value in _VALUES}
_FIELD_NAMES = {value: frozenset(field.name for field in schema) for value, schema in _VALUE_FIELDS.items()}
_ENUM_TYPES = frozenset(_ENUMS)
_EVOLVED_TAGS = {"WorkflowSnapshotV2": "WorkflowSnapshot"}

# Explicit evolution of already-persisted variants. Never infer defaults from
# dataclass declarations: new authority fields require a deliberate read rule.
_LEGACY_FIELDS: dict[str, dict[str, object]] = {
    "SettlementRequest": {"accounting_gap": "", "origin": {"type": "ChargeOrigin", "value": "normal_return"}},
    "WorkflowSnapshot": {
        "issue_recovery": {"type": "InactiveIssueRecovery", "fields": {}},
    },
    # Histories persisted before re-executions were counted had spent none.
    "ActiveReconciliation": {"reexecutions": 0},
    "PRRepairState": {"resumes": 0, "actual_model": None, "blank_reply": None, "ci_rerun": None, "ci_cancel": None, "branch_refresh": None, "pending_ci_escalation": None},
    "PRRepairPurchase": {"branch": "", "handoff": None, "advance_resume": False, "head": ""},
    "PRRepairResult": {"ci": None, "review": None, "rebase": None},
    "IssueRecoveryState": {"pending_ci": None, "pending_rebase": None},
    "ResolveAdmission": {"workspace_handoff_id": None},
    "RequestRearm": {"expected_binding": None},
    "ContinueGrantedRearm": {"expected_binding": None},
    "CancelRearm": {"expected_binding": None},
    "AuthorizeAbsentRetry": {"expected_binding": None},
}

# Self-clear state (machine/budget holds and their allowance) and the fix
# infrastructure ceiling were deleted without a migration; rows persisted
# before then still carry them. Dropped before decoding, so their retired
# nested variants are never constructed.
_RETIRED_FIELDS: dict[str, frozenset[str]] = {
    "IssueRecoveryState": frozenset({"self_clears", "machine_block", "budget_block", "self_clear_ceiling_reported",
                                     "infra_failures", "infra_episode"}),
    "ContinuationCapture": frozenset({"fix_infra_ceiling"}),
    "PRRepairState": frozenset({"self_clears"}),
    "PRRepairResult": frozenset({"machine_block"}),
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
            "type": ("WorkflowSnapshotV2" if type(value) is w.WorkflowSnapshot and (
                         not isinstance(value.issue_recovery, cc.InactiveIssueRecovery))
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
                    and not (type(value) is cc.IssueRecoveryState and field.name in {"pending_ci", "pending_rebase"} and getattr(value, field.name) is None)
                    and not (type(value) is SettlementRequest and field.name == "accounting_gap" and not value.accounting_gap)
                    and not (type(value) is SettlementRequest and field.name == "origin" and value.origin is ChargeOrigin.NORMAL_RETURN)
                    and not (type(value) is cc.PRRepairResult and (field.name == "ci" and value.ci is None
                        or field.name == "review" and value.review is None
                        or field.name == "rebase" and value.rebase is None))
                    and not (type(value) is cc.PRRepairPurchase and (field.name == "branch" and not value.branch
                        or field.name == "handoff" and value.handoff is None
                        or field.name == "advance_resume" and not value.advance_resume
                        or field.name == "head" and not value.head))
                    and (field.name not in {"expected_binding", "lineage_sha256"}
                     or getattr(value, field.name) is not None)
                    and not (field.name == "issue_recovery"
                             and type(value) is w.WorkflowSnapshot and isinstance(value.issue_recovery, cc.InactiveIssueRecovery)))
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
    strict_evolution = tag in _EVOLVED_TAGS
    if strict_evolution:
        tag = _EVOLVED_TAGS[tag]
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
    if tag in _RETIRED_FIELDS:
        payload = {key: item for key, item in payload.items() if key not in _RETIRED_FIELDS[tag]}
    legacy: dict[str, object] = {} if strict_evolution else _LEGACY_FIELDS.get(tag, {})
    field_names = _FIELD_NAMES[constructor]
    required = field_names - legacy.keys()
    if tag in {"SettlementRequest", "PRRepairState", "PRRepairPurchase", "PRRepairResult", "IssueRecoveryState"} and required <= set(payload) <= field_names:
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
