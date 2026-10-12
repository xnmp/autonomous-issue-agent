"""Closed operator commands; wire data cannot mint workflow evidence or actors."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, field_validator

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


class OperatorExpected(ClosedContract):
    family: Literal["resolve", "review_rejections"]
    repository: Identifier
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

    def key(self) -> WorkflowKey:
        raise NotImplementedError

    def binding(self) -> WorkflowBinding:
        return WorkflowBinding(WorkflowFence(self.key(), self.generation),
            FamilyAuthority(WorkflowFamily(self.family), self.epoch), self.episode_id)

    def revision(self) -> WorkflowRevision:
        return WorkflowRevision(self.binding().fence, self.version)


class ResolveExpected(OperatorExpected):
    issue: Positive
    family: Literal["resolve"]

    def key(self) -> WorkflowKey:
        return WorkflowKey(self.repository, IssueRef(self.issue))


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


class ResumeGate(ClosedContract):
    kind: Literal["resume"]
    guidance: Guidance


class RenewExecution(ResumeGate):
    kind: Literal["renew_execution"]
    execution_epoch: Positive
    execution_episode: Identifier


class RetryAbsent(ClosedContract):
    kind: Literal["retry_absent"]
    command_id: Identifier
    evidence_id: Identifier


class GateSelection(ClosedContract):
    kind: Literal["gate"]
    gate_id: Identifier
    intent: Annotated[ResumeGate | RenewExecution | RetryAbsent, Field(discriminator="kind")]


class VerdictSelection(RenewExecution):
    kind: Literal["verdict"]
    gate_id: Identifier
    submission_command_id: Identifier


class StoppedGrantSelection(ClosedContract):
    kind: Literal["stopped_grant"]
    stopped_request_id: Identifier


class ReleaseBody(ClosedContract):
    operation: Literal["release"]
    request_id: Identifier
    expected: OperatorExpected
    selection: Annotated[GateSelection | VerdictSelection | StoppedGrantSelection, Field(discriminator="kind")]

    def command(self) -> w.OperatorCommand:
        expected = self.expected.revision()
        binding = self.expected.binding() if self.expected.family == "review_rejections" else None
        selected = self.selection
        if isinstance(selected, StoppedGrantSelection):
            return w.ContinueGrantedRearm(self.request_id, expected, selected.stopped_request_id, expected_binding=binding)
        elif isinstance(selected, GateSelection) and isinstance(selected.intent, RetryAbsent):
            return w.AuthorizeAbsentRetry(self.request_id, expected, w.CommandId(selected.intent.command_id),
                selected.gate_id, selected.intent.evidence_id, expected_binding=binding)
        else:
            intent = selected.intent if isinstance(selected, GateSelection) else selected
            assert isinstance(intent, ResumeGate)
            guidance = (w.SetGuidance(intent.guidance.text) if isinstance(intent.guidance, SetGuidance)
                        else w.ClearGuidance() if isinstance(intent.guidance, ClearGuidance) else w.KeepGuidance())
            if isinstance(selected, VerdictSelection):
                return w.RequestReviewVerdictRelease(self.request_id, expected, selected.gate_id, guidance,
                    selected.execution_epoch, selected.execution_episode, w.CommandId(selected.submission_command_id), expected_binding=binding)
            elif isinstance(intent, RenewExecution):
                return w.RequestReviewExecutionRenewal(self.request_id, expected, selected.gate_id, guidance,
                    intent.execution_epoch, intent.execution_episode, expected_binding=binding)
            else:
                return w.RequestRearm(self.request_id, expected, selected.gate_id, guidance, expected_binding=binding)


class ResolveGateSelection(GateSelection):
    intent: Annotated[ResumeGate | RetryAbsent, Field(discriminator="kind")]


class ResolveRelease(ReleaseBody):
    expected: ResolveExpected
    selection: Annotated[ResolveGateSelection | StoppedGrantSelection, Field(discriminator="kind")]


class Cancellation(ClosedContract):
    operation: Literal["cancel_rearm"]
    expected: OperatorExpected
    cancellation_id: Identifier
    request_id: Identifier
    reason: Annotated[str, Field(min_length=1, max_length=16384)]

    def command(self) -> w.CancelRearm:
        return w.CancelRearm(self.cancellation_id, self.expected.revision(), self.request_id, self.reason,
            expected_binding=self.expected.binding() if self.expected.family == "review_rejections" else None)



class CancelRearm(Cancellation):
    expected: ResolveExpected


def legacy_release(value, release_type):
    """Tabs opened before deployment still submit the original HTTP operations.

    Temporary (#298): delete with increment 2. By then every open tab has
    reloaded the `release` client, and saved browser retries are already
    rewritten to `release` on load.
    """
    if not isinstance(value, dict) or value.get("operation") in ("release", "cancel_rearm"):
        return value
    operation = value.get("operation")
    shapes = {
        "request_rearm": ("gate", "resume", "request_id", ("gate_id", "guidance")),
        "renew_execution": ("gate", "renew_execution", "request_id", ("gate_id", "guidance", "execution_epoch", "execution_episode")),
        "release_verdict": ("verdict", None, "request_id", ("gate_id", "guidance", "execution_epoch", "execution_episode", "submission_command_id")),
        "authorize_retry": ("gate", "retry_absent", "authorization_id", ("gate_id", "command_id", "evidence_id")),
        "continue_rearm": ("stopped_grant", None, "request_id", ("stopped_request_id",)),
    }
    if not isinstance(operation, str) or operation not in shapes:
        raise ValueError("unknown operator operation")
    kind, intent, identity, fields = shapes[operation]
    if set(value) != {"operation", "expected", identity, *fields}:
        raise ValueError("legacy operation has missing or unknown fields")
    selected = {key: item for key, item in value.items() if key not in ("operation", "expected", identity)}
    if intent is not None:
        selected = {"gate_id": selected.pop("gate_id"), "intent": {"kind": intent, **selected}}
    return release_type.model_validate({"operation": "release", "expected": value.get("expected"),
        "request_id": value.get(identity), "selection": {"kind": kind, **selected}})


ResolveCommand = Annotated[ResolveRelease | CancelRearm, Field(discriminator="operation"),
                           BeforeValidator(lambda value: legacy_release(value, ResolveRelease))]


def requested_rearm(body: ResolveRelease | CancelRearm) -> str | None:
    return None if isinstance(body.command(), w.AuthorizeAbsentRetry) else body.request_id


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


class IssueRecoveryAvailable(ClosedContract):
    status: Literal["available"]
    family: Literal["issue_recovery"]
    generation: Counter
    version: Counter
    resumes: Counter
    ci_failures: Counter
    review_failures: Counter
    rebase_failures: Counter
    holds: list[Literal["workflow"]]


class PRRepairAvailable(ClosedContract):
    status: Literal["available"]
    family: Literal["pr_repair"]
    generation: Counter
    version: Counter
    resumes: Counter
    workflow_held: bool
    pending_effects: list[Literal["ci_rerun", "ci_cancel", "branch_refresh", "pending_ci_escalation"]]


class RecoveryUnavailable(ClosedContract):
    status: Literal["inactive", "migration_required", "unavailable"]
    reason: str


RecoveryControl = IssueRecoveryAvailable | PRRepairAvailable | RecoveryUnavailable


class AvailableControl(ClosedContract):
    fix_recovery: RecoveryControl | None = None
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
    fix_recovery: RecoveryControl | None = None
    status: Literal["inactive"]
    repository: str
    issue: int


class AbsentControl(ClosedContract):
    fix_recovery: RecoveryControl | None = None
    status: Literal["absent"]
    repository: str
    issue: int
    epoch: int


class UnavailableControl(ClosedContract):
    fix_recovery: RecoveryControl | None = None
    status: Literal["unavailable"]
    reason: str


ResolveControl = Annotated[AvailableControl | InactiveControl | AbsentControl | UnavailableControl, Field(discriminator="status")]


class CommandResult(ClosedContract):
    outcome: Literal["accepted", "replayed", "conflict", "rejected", "unavailable"]
    reason: str
    control: ResolveControl
