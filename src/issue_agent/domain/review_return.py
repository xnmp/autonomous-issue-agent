"""Unclassified review child output retained before outer-round decisions."""

from dataclasses import dataclass
from datetime import datetime
import json

from .agent_task import RuntimeName
from .outcome import AgentResult, ExitReason
from .review_execution import ReviewChildAdmission, ReviewChildIdentity
from .workflow_identity import validate_workflow_identifier as identifier

MAX_RETURN_EVIDENCE_BYTES = 200000


@dataclass(frozen=True)
class ReviewReturnedEnvelope:
    is_error: bool
    status: str
    summary: str
    needs: str
    envelope_subtype: str
    exit_reason: ExitReason | None
    session_id: str
    structured_json: str

    def __post_init__(self) -> None:
        if type(self.is_error) is not bool or (self.exit_reason is not None and not isinstance(self.exit_reason, ExitReason)):
            raise ValueError("review return requires a typed result and exit reason")
        values = (self.status, self.summary, self.needs, self.envelope_subtype, self.session_id, self.structured_json)
        if any(not isinstance(value, str) or "\x00" in value for value in values):
            raise ValueError("review return text must be valid strings")
        if len(self.status) > 512 or len(self.envelope_subtype) > 512 or len(self.session_id) > 512:
            raise ValueError("review return status, subtype or session exceeds its bound")
        if sum(len(value.encode("utf-8")) for value in values) > MAX_RETURN_EVIDENCE_BYTES:
            raise ValueError("review return exceeds durable evidence capacity")
        try:
            structured = json.loads(self.structured_json)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("review return structured output is not JSON") from exc
        if structured is not None and not isinstance(structured, dict):
            raise ValueError("review return structured output must be an object or null")
        try:
            canonical = json.dumps(structured, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("review return structured output cannot be normalized") from exc
        if canonical != self.structured_json:
            raise ValueError("review return structured output must be canonical JSON")


def review_returned_envelope(result: AgentResult) -> ReviewReturnedEnvelope:
    """Capture only bounded result fields used by later review classification."""
    if not isinstance(result, AgentResult):
        raise ValueError("review return requires a normalized agent result")
    if result.structured is not None and not isinstance(result.structured, dict):
        raise ValueError("review return structured output must be an object or null")
    try:
        structured = json.dumps(
            result.structured,
            sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("review return structured output cannot be encoded") from exc
    return ReviewReturnedEnvelope(
        result.is_error, result.status, result.summary, result.needs,
        result.envelope_subtype, result.exit_reason, result.session_id, structured,
    )


@dataclass(frozen=True)
class ReturnedReviewChild:
    child: ReviewChildIdentity
    run_id: str
    owner_id: str
    runtime: RuntimeName
    result: ReviewReturnedEnvelope
    returned_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.child, ReviewChildIdentity):
            raise ValueError("review return requires its planned child")
        identifier(self.run_id, "review return run")
        identifier(self.owner_id, "review return owner")
        if not isinstance(self.runtime, RuntimeName) or not isinstance(self.result, ReviewReturnedEnvelope):
            raise ValueError("review return requires typed runtime and result")
        if (not isinstance(self.returned_at, datetime) or self.returned_at.tzinfo is None
                or self.returned_at.utcoffset() is None):
            raise ValueError("review return requires an aware time")


@dataclass(frozen=True)
class ReviewRuntimePreflightStop:
    """Harness observation before any supervised child launch is attempted."""

    admission: ReviewChildAdmission
    receipt_id: str
    result: ReviewReturnedEnvelope
    stopped_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.admission, ReviewChildAdmission):
            raise ValueError("review preflight stop requires its paid child admission")
        if self.receipt_id != f"review-execution-preflight-stop:{self.admission.run_id}":
            raise ValueError("review preflight stop receipt disagrees with its run")
        if (not isinstance(self.result, ReviewReturnedEnvelope)
                or not self.result.is_error or self.result.status != "blocked"
                or self.result.exit_reason is not ExitReason.RUNTIME_UNAVAILABLE
                or self.result.needs or self.result.envelope_subtype
                or self.result.session_id or self.result.structured_json != "null"
                or not self.result.summary):
            raise ValueError("review preflight stop requires the exact local runtime failure")
        if (not isinstance(self.stopped_at, datetime) or self.stopped_at.tzinfo is None
                or self.stopped_at.utcoffset() is None):
            raise ValueError("review preflight stop requires an aware time")
