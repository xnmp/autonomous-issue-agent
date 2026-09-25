"""Closed operator commands; wire data cannot mint workflow evidence or actors."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from ...domain import workflow as w
from ...domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowBinding, WorkflowFamily, WorkflowFence,
    WorkflowKey, WorkflowRevision, canonical_workflow_repository,
    validate_workflow_identifier,
)
from .guidance import GUIDANCE_MAX_CHARS, parse_guidance


def validate_identifier(value: str) -> str:
    validate_workflow_identifier(value, "identifier")
    value.encode("utf-8")
    return value


Identifier = Annotated[str, Field(min_length=1, max_length=512), AfterValidator(validate_identifier)]
# Browser JSON numbers must round-trip the exact authority identity.
Counter = Annotated[int, Field(ge=0, le=2**53 - 1)]
Positive = Annotated[int, Field(ge=1, le=2**53 - 1)]


class ClosedContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("*", check_fields=False)
    @classmethod
    def encodable_text(cls, value):
        if isinstance(value, str):
            value.encode("utf-8")
            if "\x00" in value:
                raise ValueError("text cannot contain NUL")
        return value


class ResolveExpected(ClosedContract):
    repository: Identifier
    issue: Positive
    family: Literal["resolve"]
    epoch: Positive
    generation: Counter
    version: Counter
    episode_id: Identifier

    @field_validator("repository")
    @classmethod
    def canonical_repository(cls, value: str) -> str:
        if canonical_workflow_repository(value) != value:
            raise ValueError("repository must be canonical")
        return value

    def binding(self) -> WorkflowBinding:
        return WorkflowBinding(
            WorkflowFence(WorkflowKey(self.repository, IssueRef(self.issue)), self.generation),
            FamilyAuthority(WorkflowFamily.RESOLVE, self.epoch), self.episode_id,
        )

    def revision(self) -> WorkflowRevision:
        return WorkflowRevision(self.binding().fence, self.version)


class KeepGuidance(ClosedContract):
    kind: Literal["keep"]


class ClearGuidance(ClosedContract):
    kind: Literal["clear"]


class SetGuidance(ClosedContract):
    kind: Literal["set"]
    text: Annotated[str, Field(min_length=1, max_length=GUIDANCE_MAX_CHARS)]

    @field_validator("text")
    @classmethod
    def valid_guidance(cls, value: str) -> str:
        parsed = parse_guidance(value)
        if not parsed:
            raise ValueError("set guidance requires text; use clear to remove it")
        return parsed


Guidance = Annotated[KeepGuidance | ClearGuidance | SetGuidance, Field(discriminator="kind")]


class RequestRearm(ClosedContract):
    operation: Literal["request_rearm"]
    expected: ResolveExpected
    request_id: Identifier
    gate_id: Identifier
    guidance: Guidance

    def command(self) -> w.RequestRearm:
        guidance = (w.SetGuidance(self.guidance.text) if isinstance(self.guidance, SetGuidance)
                    else w.ClearGuidance() if isinstance(self.guidance, ClearGuidance) else w.KeepGuidance())
        return w.RequestRearm(self.request_id, self.expected.revision(), self.gate_id, guidance)


class ContinueRearm(ClosedContract):
    operation: Literal["continue_rearm"]
    expected: ResolveExpected
    request_id: Identifier
    stopped_request_id: Identifier

    def command(self) -> w.ContinueGrantedRearm:
        return w.ContinueGrantedRearm(self.request_id, self.expected.revision(), self.stopped_request_id)


class CancelRearm(ClosedContract):
    operation: Literal["cancel_rearm"]
    expected: ResolveExpected
    cancellation_id: Identifier
    request_id: Identifier
    reason: Annotated[str, Field(min_length=1, max_length=16384)]

    def command(self) -> w.CancelRearm:
        return w.CancelRearm(self.cancellation_id, self.expected.revision(), self.request_id, self.reason)


class AuthorizeRetry(ClosedContract):
    """One execution of an existing command; the evidence is the harness's own read."""
    operation: Literal["authorize_retry"]
    expected: ResolveExpected
    authorization_id: Identifier
    gate_id: Identifier
    command_id: Identifier
    evidence_id: Identifier

    def command(self) -> w.AuthorizeAbsentRetry:
        return w.AuthorizeAbsentRetry(
            self.authorization_id, self.expected.revision(), w.CommandId(self.command_id),
            self.gate_id, self.evidence_id,
        )


ResolveCommand = Annotated[RequestRearm | ContinueRearm | CancelRearm | AuthorizeRetry, Field(discriminator="operation")]


def requested_rearm(body: RequestRearm | ContinueRearm | CancelRearm | AuthorizeRetry) -> str | None:
    """The re-arm history a command result should display, if it names one."""
    return None if isinstance(body, AuthorizeRetry) else body.request_id


class ResolveCeiling(ClosedContract):
    kind: Literal["resolve_ceiling"]
    episode_id: str
    failures: int
    ceiling: int
    last_failure: str


class RemoteUncertain(ClosedContract):
    kind: Literal["remote_uncertain"]
    command_id: str
    unresolved_since: str
    failed_reads: int
    last_error: str


class RetryAuthorizationRequired(ClosedContract):
    kind: Literal["retry_authorization_required"]
    command_id: str
    absence_evidence_id: str


class EvidenceUnavailable(ClosedContract):
    kind: Literal["evidence_unavailable"]
    source: str
    unresolved_since: str
    failed_reads: int
    last_error: str


class RearmNeedsDecision(ClosedContract):
    kind: Literal["rearm_needs_decision"]
    stopped_request_id: str
    obligations: list[str]
    reason: str


GateCause = Annotated[ResolveCeiling | RemoteUncertain | RetryAuthorizationRequired | EvidenceUnavailable | RearmNeedsDecision, Field(discriminator="kind")]


class NoRemediation(ClosedContract):
    kind: Literal["none_authorized"]
    reason: str


class AttemptedRemediation(ClosedContract):
    kind: Literal["attempted"]
    operation: str
    evidence_ids: list[str]
    outcome: str


Remediation = Annotated[NoRemediation | AttemptedRemediation, Field(discriminator="kind")]


class PendingRetry(ClosedContract):
    """One authorized retry accepted and awaiting the executor's decision."""
    authorization_id: str
    command_id: str


class Gate(ClosedContract):
    gate_id: str
    state: Literal["open", "rearming"]
    opened_generation: int
    cause: GateCause
    remediation: Remediation
    available_commands: list[Literal["request_rearm", "continue_rearm", "cancel_rearm", "authorize_retry"]]
    pending_retry: PendingRetry | None


class Rearm(ClosedContract):
    request_id: str
    state: Literal["pending", "completed", "cancelled", "superseded"]
    gate_id: str
    generation: int
    grant_request_id: str
    episode_id: str
    handoff_id: str
    branch: str
    base_ref: str
    base_commit: str
    required_commands: list[str]
    verified_commands: list[str]
    retired_commands: list[str]
    reason: str


class Observation(ClosedContract):
    unresolved_since: str
    next_read_at: str
    failed_reads: int
    last_error: str


class AvailableControl(ClosedContract):
    status: Literal["available"]
    expected: ResolveExpected
    workflow_held: bool = Field(description="SQLite workflow holds only, not a remote readiness assertion; submission rechecks external holds.")
    gates: list[Gate]
    guidance: str
    failures: int
    current_intent: Rearm | None
    requested_id: str | None
    requested_intent: Rearm | None
    observation: Observation | None


class InactiveControl(ClosedContract):
    status: Literal["inactive"]
    repository: str
    issue: int


class AbsentControl(ClosedContract):
    status: Literal["absent"]
    repository: str
    issue: int
    epoch: int


class UnavailableControl(ClosedContract):
    status: Literal["unavailable"]
    reason: str


ResolveControl = Annotated[AvailableControl | InactiveControl | AbsentControl | UnavailableControl, Field(discriminator="status")]


class CommandResult(ClosedContract):
    outcome: Literal["accepted", "replayed", "conflict", "rejected", "unavailable"]
    reason: str
    control: ResolveControl
