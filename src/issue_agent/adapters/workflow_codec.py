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
from ..domain import workflow_identity as identity
from ..domain.contracts import WorkflowAttemptIdentity
from ..domain.labels import Label
from ..domain.world import Comment, IssueView

MAX_WORKFLOW_PAYLOAD_BYTES = 262144
MAX_WORKFLOW_PAYLOAD_DEPTH = 24
T = TypeVar("T")

_VALUES = (
    identity.IssueRef, identity.PullRequestRef, identity.WorkflowKey,
    identity.WorkflowFence, identity.FamilyAuthority, identity.WorkflowRevision,
    identity.WorkflowBinding, WorkflowAttemptIdentity,
    Comment, IssueView, w.ObservedIssue, w.NoLinkedPr, w.LinkedPrPresent, w.ResolveRearmPreflight,
    w.ResolveEpisode, w.ResolveCeiling, w.RemoteOutcomeUncertain, w.RetryAuthorizationRequired,
    w.EvidenceUnavailable, w.RearmNeedsDecision, w.NoAuthorizedRemediation,
    w.RemediationAttempted, w.OpenGate, w.RearmingGate,
    w.KeepGuidance, w.ClearGuidance, w.SetGuidance, w.OperatorActor,
    w.RequestRearm, w.ContinueGrantedRearm, w.CancelRearm, w.ReconcileGate,
    w.AuthorizeAbsentRetry, w.RenewResolveAllowance, w.EnsureLifecycleProjection,
    w.PendingRearm, w.CompletedRearm, w.SupersededRearm, w.CancelledRearm,
    w.WorkflowSnapshot, w.CommandAbsenceEvidence, w.CommandRetryApplicability,
    w.StandaloneCommand, w.RearmObligation, w.UnconsumedRetryAuthorization,
    w.ConsumedRetryAuthorization, w.ActiveReconciliation, w.ReconciliationOnly,
    w.RetiredUncertainCommand, w.RetiredSettledCommand, w.ResolveAttemptResult,
    w.AuthorizedOperatorCommand, w.ObservedTransition,
)
_ENUMS = (
    Label, identity.WorkflowFamily, w.StopReason, w.RemediationOperation,
    w.RemediationOutcome, w.ResolveResultKind,
)
_TAGS = {value.__name__: value for value in (*_VALUES, *_ENUMS)}


class WorkflowEncodingError(ValueError):
    pass


def _encode(value: object, depth: int = 0) -> object:
    if depth > MAX_WORKFLOW_PAYLOAD_DEPTH:
        raise WorkflowEncodingError("workflow payload nesting is excessive")
    if type(value) in _ENUMS:
        return {"type": type(value).__name__, "value": value.value}
    if type(value) in _VALUES and is_dataclass(value):
        return {
            "type": type(value).__name__,
            "fields": {
                field.name: _encode(getattr(value, field.name), depth + 1)
                for field in fields(value)
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
