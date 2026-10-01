"""Fixed secondary continuity values; the target aggregate owns the shared fence."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from .agent_task import RuntimeName
from .refs import issue_from_branch
from .contracts import ContinuationAdmission
from .eligibility import IssueCompletionGuard, LifecycleOperation
from .workflow_identity import (
    MAX_WORKFLOW_GATES, ContinuationComponentIdentity, FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowKey, WorkflowRevision, WorkflowFence,
    validate_workflow_identifier, validate_workflow_integer, validate_workflow_text,
)

if TYPE_CHECKING:
    from .workflow import UnresolvedGate


@dataclass(frozen=True)
class ContinuationWatermarks:
    last_admitted: int = 0
    last_applied: int = 0
    last_admitted_fence: WorkflowFence | None = None

    def __post_init__(self) -> None:
        validate_workflow_integer(self.last_admitted, "last continuation admission")
        validate_workflow_integer(self.last_applied, "last applied continuation")
        if self.last_applied > self.last_admitted:
            raise ValueError("applied continuity cannot exceed its admission watermark")
        if self.last_admitted_fence is not None and not isinstance(self.last_admitted_fence, WorkflowFence):
            raise ValueError("admission watermark requires a typed historical fence")
        if self.last_admitted > self.last_applied and self.last_admitted_fence is None:
            raise ValueError("pending continuation requires its captured admission fence")
        if self.last_admitted == 0 and self.last_admitted_fence is not None:
            raise ValueError("an empty watermark has no admitted fence")

    def admit(self, version: int, fence: WorkflowFence) -> ContinuationWatermarks:
        validate_workflow_integer(version, "continuation admission version", minimum=1)
        if version <= self.last_admitted:
            raise ValueError("a new continuation admission must advance its watermark")
        return replace(self, last_admitted=version, last_admitted_fence=fence)




def _timestamp(value: str, name: str) -> None:
    validate_workflow_text(value, name, empty=True)
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an aware timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{name} must be an aware timestamp")


def reply_advances(selected: str, consumed: str) -> bool:
    _timestamp(selected, "selected reply")
    _timestamp(consumed, "consumed reply")
    return bool(selected) and (not consumed or datetime.fromisoformat(selected.replace("Z", "+00:00"))
                              > datetime.fromisoformat(consumed.replace("Z", "+00:00")))


@dataclass(frozen=True)
class RepairSession:
    source: ContinuationAdmission
    handle: str
    runtime: RuntimeName
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, ContinuationAdmission) or self.source.attempt.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise ValueError("repair session requires its PR repair admission")
        validate_workflow_identifier(self.handle, "repair session handle")
        if not isinstance(self.runtime, RuntimeName):
            raise ValueError("repair session requires a typed runtime")
        validate_workflow_text(self.model, "repair session model", empty=True)


@dataclass(frozen=True)
class IssueRecoveryState:
    consumed_reply_at: str = ""
    resumes: int = 0
    remembered_branch: str = ""
    ci_failures: int = 0
    review_failures: int = 0
    rebase_failures: int = 0
    infra_failures: int = 0
    self_clears: int = 0

    def __post_init__(self) -> None:
        _timestamp(self.consumed_reply_at, "consumed issue reply")
        validate_workflow_text(self.remembered_branch, "remembered issue branch", empty=True)
        for name in ("resumes", "ci_failures", "review_failures", "rebase_failures", "infra_failures", "self_clears"):
            validate_workflow_integer(getattr(self, name), name)


@dataclass(frozen=True)
class PRRepairState:
    consumed_reply_at: str = ""
    branch: str = ""
    session: RepairSession | None = None
    ci_failures: int = 0
    review_failures: int = 0
    rebase_failures: int = 0
    infra_failures: int = 0
    self_clears: int = 0

    def __post_init__(self) -> None:
        _timestamp(self.consumed_reply_at, "consumed PR reply")
        validate_workflow_text(self.branch, "repair branch", empty=True)
        if self.session is not None and not isinstance(self.session, RepairSession):
            raise ValueError("PR repair continuity requires a typed session")
        for name in ("ci_failures", "review_failures", "rebase_failures", "infra_failures", "self_clears"):
            validate_workflow_integer(getattr(self, name), name)


class StewardLane(str, Enum):
    TRIAGE = "triage"
    GROOM = "groom"


def _groom_signature(value: str) -> None:
    validate_workflow_text(value, "groom content signature", limit=64, empty=True)
    if value and (len(value) != 64 or any(c not in "0123456789abcdef" for c in value)):
        raise ValueError("groom content signature requires a canonical SHA-256 digest")


@dataclass(frozen=True)
class GroomContent:
    attempted: str = ""
    successful: str = ""

    def __post_init__(self) -> None:
        _groom_signature(self.attempted)
        _groom_signature(self.successful)


@dataclass(frozen=True)
class StewardAttemptSource:
    admission: ContinuationAdmission
    lane: StewardLane
    input_at: str
    content_signature: str = ""

    def __post_init__(self) -> None:
        if (not isinstance(self.admission, ContinuationAdmission)
                or self.admission.attempt.binding.authority.family is not WorkflowFamily.STEWARD
                or not isinstance(self.lane, StewardLane)):
            raise ValueError("steward delivery requires its exact family admission and lane")
        _timestamp(self.input_at, "steward delivery input")
        if not self.input_at:
            raise ValueError("steward delivery requires its admitted input baseline")
        _groom_signature(self.content_signature)
        if self.lane is StewardLane.TRIAGE and self.content_signature:
            raise ValueError("triage source cannot claim grooming content")


@dataclass(frozen=True)
class StewardCommandGuard:
    source: StewardAttemptSource
    facts: IssueCompletionGuard
    position: int
    writer_login: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.writer_login, "steward command writer login")
        if not isinstance(self.source, StewardAttemptSource) or not isinstance(self.facts, IssueCompletionGuard):
            raise ValueError("steward command requires exact admitted completion facts")
        if self.facts.operation not in {LifecycleOperation.COMPLETE_TRIAGE, LifecycleOperation.CLOSE, LifecycleOperation.GROOM}:
            raise ValueError("steward command requires fixed steward completion facts")
        from .labels import Label
        if not isinstance(self.facts.labels, frozenset) or any(not isinstance(label, Label) for label in self.facts.labels):
            raise ValueError("steward completion labels must be typed")
        for value in (self.facts.title, self.facts.body, self.facts.author, self.facts.state_reason):
            validate_workflow_text(value, "steward completion fact", limit=65536, empty=True)
        if self.source.lane is StewardLane.GROOM and self.source.content_signature:
            from .triage import grooming_content_signature
            if grooming_content_signature(self.facts.title, self.facts.body) != self.source.content_signature:
                raise ValueError("groom completion facts differ from the purchased content")
        if not isinstance(self.facts.comments, tuple) or len(self.facts.comments) > 1024:
            raise ValueError("steward completion comments must be bounded")
        for comment in self.facts.comments:
            if not isinstance(comment, tuple) or len(comment) != 3:
                raise ValueError("steward completion comment requires its exact author/body/time")
            for value in comment:
                validate_workflow_text(value, "steward completion comment", limit=65536, empty=True)
        validate_workflow_integer(self.position, "steward verdict position")
        if self.position >= MAX_WORKFLOW_GATES:
            raise ValueError("steward verdict exceeds its fixed command bound")


@dataclass(frozen=True)
class StewardLaneState:
    baseline_at: str = ""
    stalls: int = 0
    infra_failures: int = 0
    pending_delivery: tuple[str, ...] = ()
    delivery_source: StewardAttemptSource | None = None
    attempted_at: str = ""

    def __post_init__(self) -> None:
        _timestamp(self.baseline_at, "steward input baseline")
        validate_workflow_integer(self.stalls, "steward stalls")
        validate_workflow_integer(self.infra_failures, "steward infrastructure failures")
        if not isinstance(self.pending_delivery, tuple) or len(self.pending_delivery) > MAX_WORKFLOW_GATES:
            raise ValueError("steward delivery references must be bounded")
        for command in self.pending_delivery:
            validate_workflow_identifier(command, "steward delivery command")
        if len(set(self.pending_delivery)) != len(self.pending_delivery):
            raise ValueError("steward delivery commands must be distinct")
        _timestamp(self.attempted_at, "steward attempted input")
        if self.delivery_source is not None:
            if not isinstance(self.delivery_source, StewardAttemptSource) or not self.pending_delivery:
                raise ValueError("steward delivery source requires owed command references")
            if self.attempted_at != self.delivery_source.input_at:
                raise ValueError("steward delivery belongs to its exact attempted input")


@dataclass(frozen=True)
class StewardState:
    triage: StewardLaneState = StewardLaneState()
    groom: StewardLaneState = StewardLaneState()
    # Saved pre-groom import plans include the original state repr in their digest.
    groom_content: GroomContent = field(default=GroomContent(), repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.triage, StewardLaneState) or not isinstance(self.groom, StewardLaneState):
            raise ValueError("steward continuity requires fixed triage and groom lanes")
        if not isinstance(self.groom_content, GroomContent):
            raise ValueError("steward grooming requires fixed input and successful content")


@dataclass(frozen=True)
class ContinuationImportBlocker:
    source: str
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.source, "continuation import source")
        validate_workflow_text(self.reason, "continuation import blocker")


def _blocked(key: WorkflowKey, authority: FamilyAuthority, family: WorkflowFamily,
             decision_id: str, blockers: tuple[ContinuationImportBlocker, ...]) -> None:
    from .workflow_identity import family_owns_target
    if not isinstance(key, WorkflowKey) or not isinstance(authority, FamilyAuthority) or authority.family is not family or not family_owns_target(family, key.target):
        raise ValueError("blocked continuation must name its exact target and family authority")
    validate_workflow_identifier(decision_id, "continuation migration decision")
    if not isinstance(blockers, tuple) or not 1 <= len(blockers) <= MAX_WORKFLOW_GATES or any(not isinstance(x, ContinuationImportBlocker) for x in blockers):
        raise ValueError("blocked continuation requires bounded source-specific blockers")


def _active(identity: ContinuationComponentIdentity, family: WorkflowFamily, state: object, state_type: type,
            watermarks: ContinuationWatermarks, gates: tuple[UnresolvedGate, ...]) -> None:
    from .workflow import OpenGate, RearmingGate
    if not isinstance(identity, ContinuationComponentIdentity) or identity.authority.family is not family or not isinstance(state, state_type) or not isinstance(watermarks, ContinuationWatermarks):
        raise ValueError("active continuation requires its fixed family identity and typed state")
    if not isinstance(gates, tuple) or len(gates) > MAX_WORKFLOW_GATES or any(not isinstance(x, (OpenGate, RearmingGate)) for x in gates):
        raise ValueError("continuation holds must be a bounded typed tuple")
    opened = tuple(x.gate if isinstance(x, RearmingGate) else x for x in gates)
    if len({x.gate_id for x in opened}) != len(opened):
        raise ValueError("continuation holds must have distinct identities")


@dataclass(frozen=True)
class InactiveIssueRecovery:
    pass


@dataclass(frozen=True)
class BlockedIssueRecovery:
    key: WorkflowKey
    authority: FamilyAuthority
    decision_id: str
    blockers: tuple[ContinuationImportBlocker, ...]

    def __post_init__(self) -> None:
        _blocked(self.key, self.authority, WorkflowFamily.ISSUE_RECOVERY, self.decision_id, self.blockers)


@dataclass(frozen=True)
class ActiveIssueRecovery:
    identity: ContinuationComponentIdentity
    state: IssueRecoveryState
    watermarks: ContinuationWatermarks = ContinuationWatermarks()
    gates: tuple[UnresolvedGate, ...] = ()

    def __post_init__(self) -> None:
        _active(self.identity, WorkflowFamily.ISSUE_RECOVERY, self.state, IssueRecoveryState, self.watermarks, self.gates)


@dataclass(frozen=True)
class InactivePRRepair:
    pass


@dataclass(frozen=True)
class BlockedPRRepair:
    key: WorkflowKey
    authority: FamilyAuthority
    decision_id: str
    blockers: tuple[ContinuationImportBlocker, ...]

    def __post_init__(self) -> None:
        _blocked(self.key, self.authority, WorkflowFamily.PR_REPAIR, self.decision_id, self.blockers)


@dataclass(frozen=True)
class ActivePRRepair:
    identity: ContinuationComponentIdentity
    state: PRRepairState
    watermarks: ContinuationWatermarks = ContinuationWatermarks()
    gates: tuple[UnresolvedGate, ...] = ()

    def __post_init__(self) -> None:
        _active(self.identity, WorkflowFamily.PR_REPAIR, self.state, PRRepairState, self.watermarks, self.gates)
        if self.state.session is not None:
            source = self.state.session.source
            prior = source.attempt.binding
            if (prior.fence.key != self.identity.key or prior.authority != self.identity.authority
                    or prior.episode_id != self.identity.episode_id
                    or source.admission_version > self.watermarks.last_applied):
                raise ValueError("repair session disagrees with applied component provenance")


@dataclass(frozen=True)
class InactiveSteward:
    pass


@dataclass(frozen=True)
class BlockedSteward:
    key: WorkflowKey
    authority: FamilyAuthority
    decision_id: str
    blockers: tuple[ContinuationImportBlocker, ...]

    def __post_init__(self) -> None:
        _blocked(self.key, self.authority, WorkflowFamily.STEWARD, self.decision_id, self.blockers)


@dataclass(frozen=True)
class ActiveSteward:
    identity: ContinuationComponentIdentity
    state: StewardState
    watermarks: ContinuationWatermarks = ContinuationWatermarks()
    gates: tuple[UnresolvedGate, ...] = ()

    def __post_init__(self) -> None:
        _active(self.identity, WorkflowFamily.STEWARD, self.state, StewardState, self.watermarks, self.gates)


IssueRecoveryComponent = InactiveIssueRecovery | BlockedIssueRecovery | ActiveIssueRecovery
PRRepairComponent = InactivePRRepair | BlockedPRRepair | ActivePRRepair
StewardComponent = InactiveSteward | BlockedSteward | ActiveSteward


def validate_component_revision(component: object, revision: WorkflowRevision, family: WorkflowFamily) -> None:
    types: tuple[type, ...]
    if family is WorkflowFamily.ISSUE_RECOVERY:
        types = (InactiveIssueRecovery, BlockedIssueRecovery, ActiveIssueRecovery)
    elif family is WorkflowFamily.PR_REPAIR:
        types = (InactivePRRepair, BlockedPRRepair, ActivePRRepair)
    elif family is WorkflowFamily.STEWARD:
        types = (InactiveSteward, BlockedSteward, ActiveSteward)
    else:
        raise ValueError("continuation family must be one of the fixed components")
    if not isinstance(revision, WorkflowRevision) or not isinstance(component, types):
        raise ValueError("aggregate requires its fixed typed continuation component")
    if isinstance(component, (BlockedIssueRecovery, BlockedPRRepair, BlockedSteward)) and component.key != revision.fence.key:
        raise ValueError("blocked component belongs to a different target")
    if isinstance(component, (ActiveIssueRecovery, ActivePRRepair, ActiveSteward)) and (
        component.identity.key != revision.fence.key
        or component.watermarks.last_admitted > revision.version
    ):
        raise ValueError("component cannot alias a target or originate in a future revision")

    if isinstance(component, (ActiveIssueRecovery, ActivePRRepair, ActiveSteward)):
        admitted = component.watermarks.last_admitted_fence
        if admitted is not None and (admitted.key != revision.fence.key or admitted.generation > revision.fence.generation):
            raise ValueError("component admission fence belongs to another target or future generation")
        from .workflow import RearmingGate
        if any((gate.gate if isinstance(gate, RearmingGate) else gate).opened_generation > revision.fence.generation
               for gate in component.gates):
            raise ValueError("component hold cannot originate in a future target generation")
    if isinstance(component, ActivePRRepair) and component.state.session is not None:
        if component.state.session.source.attempt.binding.fence.generation > revision.fence.generation:
            raise ValueError("repair session cannot originate in a future target generation")


@dataclass(frozen=True)
class SelectedIssueRecovery:
    revision: WorkflowRevision
    component: ActiveIssueRecovery

    def __post_init__(self) -> None:
        if not isinstance(self.component, ActiveIssueRecovery):
            raise ValueError("issue recovery selection requires active authority")
        validate_component_revision(self.component, self.revision, WorkflowFamily.ISSUE_RECOVERY)

    @property
    def binding(self) -> WorkflowBinding:
        return self.component.identity.bind(self.revision.fence)


@dataclass(frozen=True)
class SelectedPRRepair:
    revision: WorkflowRevision
    component: ActivePRRepair

    def __post_init__(self) -> None:
        if not isinstance(self.component, ActivePRRepair):
            raise ValueError("PR repair selection requires active authority")
        validate_component_revision(self.component, self.revision, WorkflowFamily.PR_REPAIR)

    @property
    def binding(self) -> WorkflowBinding:
        return self.component.identity.bind(self.revision.fence)


@dataclass(frozen=True)
class SelectedSteward:
    revision: WorkflowRevision
    component: ActiveSteward

    def __post_init__(self) -> None:
        if not isinstance(self.component, ActiveSteward):
            raise ValueError("steward selection requires active authority")
        validate_component_revision(self.component, self.revision, WorkflowFamily.STEWARD)

    @property
    def binding(self) -> WorkflowBinding:
        return self.component.identity.bind(self.revision.fence)


IssueRecoverySelection = InactiveIssueRecovery | BlockedIssueRecovery | SelectedIssueRecovery
PRRepairSelection = InactivePRRepair | BlockedPRRepair | SelectedPRRepair
StewardSelection = InactiveSteward | BlockedSteward | SelectedSteward


def select_issue_recovery(aggregate) -> IssueRecoverySelection:
    from .workflow import IssueWorkflowShell, WorkflowSnapshot
    if not isinstance(aggregate, (WorkflowSnapshot, IssueWorkflowShell)):
        raise ValueError("issue recovery selection requires an issue aggregate")
    component = aggregate.issue_recovery
    return SelectedIssueRecovery(aggregate.revision, component) if isinstance(component, ActiveIssueRecovery) else component


def select_pr_repair(aggregate) -> PRRepairSelection:
    from .review_workflow import ReviewWorkflowSnapshot
    if not isinstance(aggregate, ReviewWorkflowSnapshot):
        raise ValueError("PR repair selection requires a PR aggregate")
    component = aggregate.pr_repair
    return SelectedPRRepair(aggregate.revision, component) if isinstance(component, ActivePRRepair) else component


def select_steward(aggregate) -> StewardSelection:
    from .workflow import IssueWorkflowShell, WorkflowSnapshot
    if not isinstance(aggregate, (WorkflowSnapshot, IssueWorkflowShell)):
        raise ValueError("steward selection requires an issue aggregate")
    component = aggregate.steward
    return SelectedSteward(aggregate.revision, component) if isinstance(component, ActiveSteward) else component


def continuation_return_is_current(selection: SelectedIssueRecovery | SelectedPRRepair | SelectedSteward,
                                   admission: ContinuationAdmission) -> bool:
    if not isinstance(selection, (SelectedIssueRecovery, SelectedPRRepair, SelectedSteward)) or not isinstance(admission, ContinuationAdmission):
        raise ValueError("continuation return requires a live selection and typed admission")
    watermarks = selection.component.watermarks
    return (admission.attempt.binding == selection.binding and admission.admission_version == watermarks.last_admitted
            and admission.admission_version > watermarks.last_applied
            and watermarks.last_admitted_fence == admission.attempt.binding.fence)


@dataclass(frozen=True)
class ContinuationCapture:
    binding: WorkflowBinding
    revision: WorkflowRevision
    leg_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WorkflowBinding) or self.binding.authority.family not in (
            WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR, WorkflowFamily.STEWARD,
        ):
            raise ValueError("continuation capture requires a fixed secondary binding")
        if not isinstance(self.revision, WorkflowRevision) or self.binding.fence != self.revision.fence:
            raise ValueError("continuation capture requires its exact aggregate revision")
        validate_workflow_identifier(self.leg_id, "continuation leg")


class RepairAllowance(str, Enum):
    CI = "ci_failures"
    REVIEW = "review_failures"
    REBASE = "rebase_failures"
    INFRA = "infra_failures"
    SELF_CLEAR = "self_clears"


@dataclass(frozen=True)
class IssueRecoveryPurchase:
    capture: ContinuationCapture
    reply_at: str
    branch: str
    resume_ceiling: int

    def __post_init__(self) -> None:
        if not isinstance(self.capture, ContinuationCapture) or self.capture.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY:
            raise ValueError("issue recovery purchase requires its family capture")
        _timestamp(self.reply_at, "selected issue reply")
        if not self.reply_at:
            raise ValueError("issue recovery purchase requires its selected reply")
        validate_workflow_text(self.branch, "selected issue branch", empty=True)
        validate_workflow_integer(self.resume_ceiling, "resume ceiling", minimum=1)


def _validate_reply_input(selection: SelectedIssueRecovery, reply_at: str, body: str) -> None:
    if not isinstance(selection, SelectedIssueRecovery):
        raise ValueError("issue reply requires selected current continuation authority")
    _timestamp(reply_at, "selected issue reply")
    if not reply_at or not reply_advances(reply_at, selection.component.state.consumed_reply_at):
        raise ValueError("issue reply is absent or already consumed")
    validate_workflow_text(body, "selected issue reply body", limit=65536)
    if not body.strip():
        raise ValueError("paid issue reply must have nonblank content")


@dataclass(frozen=True)
class IssueReplyInput:
    selection: SelectedIssueRecovery
    reply_at: str
    body: str
    branch: str

    def __post_init__(self) -> None:
        _validate_reply_input(self.selection, self.reply_at, self.body)
        validate_workflow_text(self.branch, "selected issue reply branch")
        if issue_from_branch(self.branch) != self.selection.binding.fence.key.target.number:
            raise ValueError("selected issue branch belongs to another target")


@dataclass(frozen=True)
class LinkedPRIssueReplyInput:
    selection: SelectedIssueRecovery
    reply_at: str
    body: str
    pr: PullRequestRef

    def __post_init__(self) -> None:
        if not isinstance(self.pr, PullRequestRef):
            raise ValueError("linked PR reply requires its observed PR identity")
        _validate_reply_input(self.selection, self.reply_at, self.body)


@dataclass(frozen=True)
class IssueReplyPurchase:
    capture: ContinuationCapture
    reply_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.capture, ContinuationCapture) or self.capture.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY:
            raise ValueError("issue reply purchase requires its family capture")
        _timestamp(self.reply_at, "selected issue reply")
        if not self.reply_at:
            raise ValueError("issue reply purchase requires its selected reply")


@dataclass(frozen=True)
class PRRepairPurchase:
    capture: ContinuationCapture
    reply_at: str = ""
    allowance: RepairAllowance = RepairAllowance.INFRA
    ceiling: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.capture, ContinuationCapture) or self.capture.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise ValueError("PR repair purchase requires its family capture")
        _timestamp(self.reply_at, "selected PR reply")
        if not isinstance(self.allowance, RepairAllowance):
            raise ValueError("PR repair purchase requires a fixed allowance")
        validate_workflow_integer(self.ceiling, "repair allowance ceiling", minimum=1)


@dataclass(frozen=True)
class StewardPurchase:
    capture: ContinuationCapture
    lane: StewardLane
    baseline_at: str
    stall_ceiling: int
    content_signature: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.capture, ContinuationCapture) or self.capture.binding.authority.family is not WorkflowFamily.STEWARD:
            raise ValueError("steward purchase requires its family capture")
        if not isinstance(self.lane, StewardLane):
            raise ValueError("steward purchase requires a fixed lane")
        _timestamp(self.baseline_at, "selected steward baseline")
        validate_workflow_integer(self.stall_ceiling, "steward stall ceiling", minimum=1)
        _groom_signature(self.content_signature)
        if self.lane is StewardLane.TRIAGE and self.content_signature:
            raise ValueError("triage purchase cannot claim grooming content")


@dataclass(frozen=True)
class StewardInput:
    selection: SelectedSteward
    lane: StewardLane
    input_at: str
    stall_ceiling: int
    content_signature: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectedSteward):
            raise ValueError("steward input requires current selected authority")
        StewardPurchase(ContinuationCapture(self.selection.binding, self.selection.revision, "input"),
                        self.lane, self.input_at, self.stall_ceiling, self.content_signature)
        if not self.input_at:
            raise ValueError("steward input requires a nonempty baseline")


class StewardFailureKind(str, Enum):
    PAID = "paid_attempt"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True)
class StewardFailed:
    source: StewardAttemptSource
    kind: StewardFailureKind

    def __post_init__(self) -> None:
        if not isinstance(self.source, StewardAttemptSource) or not isinstance(self.kind, StewardFailureKind):
            raise ValueError("steward failure requires its exact admitted source and classification")


@dataclass(frozen=True)
class StewardVerdict:
    source: StewardAttemptSource
    commands: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, StewardAttemptSource):
            raise ValueError("steward verdict requires its exact admitted source")
        if not isinstance(self.commands, tuple) or not 1 <= len(self.commands) <= MAX_WORKFLOW_GATES:
            raise ValueError("steward verdict requires bounded owed command references")
        for command in self.commands:
            validate_workflow_identifier(command, "steward verdict command")
        if len(set(self.commands)) != len(self.commands):
            raise ValueError("steward verdict command references must be distinct")


@dataclass(frozen=True)
class StewardStopped:
    source: StewardAttemptSource
    commands: tuple[str, ...]

    def __post_init__(self) -> None:
        StewardVerdict(self.source, self.commands)


@dataclass(frozen=True)
class GroomNoFinding:
    source: StewardAttemptSource

    def __post_init__(self) -> None:
        if (not isinstance(self.source, StewardAttemptSource) or self.source.lane is not StewardLane.GROOM
                or not self.source.content_signature):
            raise ValueError("groom no-finding requires its exact admitted content")


StewardReturned = StewardFailed | StewardVerdict | StewardStopped | GroomNoFinding


def steward_return_state(selection: SelectedSteward, outcome: StewardReturned) -> StewardState | None:
    if not isinstance(outcome, (StewardFailed, StewardVerdict, StewardStopped, GroomNoFinding)):
        raise ValueError("steward return requires a classified immutable outcome")
    source = outcome.source
    if not continuation_return_is_current(selection, source.admission):
        return None
    lane = selection.component.state.triage if source.lane is StewardLane.TRIAGE else selection.component.state.groom
    if (lane.attempted_at != source.input_at or lane.pending_delivery
            or source.lane is StewardLane.GROOM and selection.component.state.groom_content.attempted != source.content_signature):
        raise ValueError("steward return disagrees with its selected input episode")
    if isinstance(outcome, GroomNoFinding):
        lane = replace(lane, baseline_at=source.input_at, stalls=0)
    elif isinstance(outcome, (StewardVerdict, StewardStopped)):
        lane = replace(lane, pending_delivery=outcome.commands, delivery_source=source,
                       stalls=lane.stalls + isinstance(outcome, StewardStopped))
    else:
        lane = replace(lane, stalls=lane.stalls + (outcome.kind is StewardFailureKind.PAID))
    state = replace(selection.component.state, **{source.lane.value: lane})
    return replace(state, groom_content=replace(state.groom_content, successful=source.content_signature)) if isinstance(outcome, GroomNoFinding) else state


def _steward_delivery_lane(selection: SelectedSteward, source: StewardAttemptSource, delivered_at: str) -> StewardLaneState | None:
    if not isinstance(selection, SelectedSteward) or not isinstance(source, StewardAttemptSource):
        raise ValueError("steward delivery requires a selected source")
    lane = selection.component.state.triage if source.lane is StewardLane.TRIAGE else selection.component.state.groom
    watermarks = selection.component.watermarks
    if (lane.delivery_source != source or not lane.pending_delivery
            or selection.binding != source.admission.attempt.binding
            or watermarks.last_admitted != source.admission.admission_version
            or watermarks.last_applied != source.admission.admission_version):
        return None
    _timestamp(delivered_at, "delivered steward baseline")
    if not delivered_at or reply_advances(source.input_at, delivered_at) or reply_advances(lane.baseline_at, delivered_at):
        raise ValueError("steward delivery cannot move its baseline backwards")
    return lane


def steward_delivered_state(selection: SelectedSteward, source: StewardAttemptSource, delivered_at: str) -> StewardState | None:
    lane = _steward_delivery_lane(selection, source, delivered_at)
    if lane is None:
        return None
    lane = replace(lane, baseline_at=delivered_at, stalls=0, pending_delivery=(), delivery_source=None)
    state = replace(selection.component.state, **{source.lane.value: lane})
    return replace(state, groom_content=replace(state.groom_content, successful=source.content_signature)) if source.lane is StewardLane.GROOM else state


def steward_stopped_state(selection: SelectedSteward, source: StewardAttemptSource, own_notice_at: str | None,
                          *, lost_verdict: bool = False) -> StewardState | None:
    lane = _steward_delivery_lane(selection, source, own_notice_at or source.input_at)
    if lane is None:
        return None
    lane = replace(lane, pending_delivery=(), delivery_source=None, stalls=lane.stalls + lost_verdict,
                   attempted_at=own_notice_at or lane.attempted_at)
    return replace(selection.component.state, **{source.lane.value: lane})


def steward_completion_matches(guard: StewardCommandGuard, issue, predecessors: tuple, *, partial=None,
                               delivered_comments: tuple = ()) -> bool:
    """Only exact receipted predecessors may change the admitted decision facts."""
    from .actions import AddDependency, CloseIssue, PostComment, Relabel
    from .lifecycle import completion_matches
    if not isinstance(guard, StewardCommandGuard) or not isinstance(predecessors, tuple) or len(predecessors) != guard.position:
        raise ValueError("steward completion requires its exact confirmed prefix")
    labels = guard.facts.labels
    expected_comments = []
    for action in predecessors:
        if isinstance(action, PostComment):
            expected_comments.append(action.body)
        elif isinstance(action, Relabel):
            labels = (labels - frozenset(action.removed_labels)) | frozenset(action.added_labels)
            if action.comment:
                expected_comments.append(action.comment)
        elif not isinstance(action, AddDependency):
            raise ValueError("steward verdict cannot mutate after an issue closure")
    remaining = list(issue.comments)
    confirmed = []
    for original in guard.facts.comments:
        found = next((comment for comment in remaining if (comment.author, comment.body, comment.created_at) == original), None)
        if found is None:
            return False
        remaining.remove(found)
        confirmed.append(found)
    if len(expected_comments) != len(delivered_comments):
        return False
    for body, delivered in zip(expected_comments, delivered_comments):
        if not isinstance(delivered, tuple) or len(delivered) != 3 or delivered[1] != body:
            return False
        found = next((comment for comment in remaining if (comment.author, comment.body, comment.created_at) == delivered), None)
        if found is None:
            return False
        remaining.remove(found)
        confirmed.append(found)
    # The original comments are already verified; confirmed additions become
    # part of the next decision's baseline, while unrelated additions refuse it.
    facts = replace(guard.facts, labels=labels,
                    comments=tuple((comment.author, comment.body, comment.created_at) for comment in confirmed))
    if isinstance(partial, PostComment):
        removed, added, comment = (), (), partial.body
    elif isinstance(partial, Relabel):
        removed, added, comment = partial.removed_labels, partial.added_labels, partial.comment
    elif isinstance(partial, CloseIssue):
        removed, added, comment = partial.remove_labels, partial.add_labels, partial.comment
    else:
        removed, added, comment = (), (), ""
    if comment and any(c.body == comment and (not guard.writer_login or c.author.casefold() != guard.writer_login.casefold())
                       for c in remaining):
        return False
    if isinstance(partial, CloseIssue) and issue.state == "closed" and issue.state_reason == partial.reason:
        issue = replace(issue, state="open", state_reason=facts.state_reason)
    return completion_matches(facts, issue, removed=removed, added=added, comment=comment)


def triage_admissible(selection: StewardSelection, input_at: str, ceiling: int) -> bool:
    if not isinstance(selection, SelectedSteward):
        return False
    try:
        consume_steward(selection, StewardPurchase(
            ContinuationCapture(selection.binding, selection.revision, "triage:preflight"),
            StewardLane.TRIAGE, input_at, ceiling), selection.revision.version + 1)
        return True
    except ValueError:
        return False


def groom_admissible(selection: StewardSelection, input_at: str, signature: str, ceiling: int) -> bool:
    if not isinstance(selection, SelectedSteward):
        return False
    try:
        consume_steward(selection, StewardPurchase(
            ContinuationCapture(selection.binding, selection.revision, "groom:preflight"),
            StewardLane.GROOM, input_at, ceiling, signature), selection.revision.version + 1)
        return True
    except ValueError:
        return False


ContinuationPurchase = IssueRecoveryPurchase | IssueReplyPurchase | PRRepairPurchase | StewardPurchase


def consume_issue_reply(selection: SelectedIssueRecovery, purchase: IssueReplyPurchase, version: int) -> ActiveIssueRecovery:
    if selection.binding != purchase.capture.binding or selection.revision != purchase.capture.revision:
        raise ValueError("issue reply capture is stale")
    component = selection.component
    if component.gates or not reply_advances(purchase.reply_at, component.state.consumed_reply_at):
        raise ValueError("issue reply is held or consumed")
    return replace(component, state=replace(component.state, consumed_reply_at=purchase.reply_at),
                   watermarks=component.watermarks.admit(version, selection.revision.fence))


def consume_issue_recovery(selection: SelectedIssueRecovery, purchase: IssueRecoveryPurchase, version: int) -> ActiveIssueRecovery:
    if selection.binding != purchase.capture.binding or selection.revision != purchase.capture.revision:
        raise ValueError("issue recovery capture is stale")
    component = selection.component
    if component.gates or not reply_advances(purchase.reply_at, component.state.consumed_reply_at) or component.state.resumes >= purchase.resume_ceiling:
        raise ValueError("issue recovery purchase is held, consumed or exhausted")
    state = replace(component.state, consumed_reply_at=purchase.reply_at,
                    resumes=component.state.resumes + 1, remembered_branch=purchase.branch)
    return replace(component, state=state, watermarks=component.watermarks.admit(version, selection.revision.fence))


def consume_pr_repair(selection: SelectedPRRepair, purchase: PRRepairPurchase, version: int) -> ActivePRRepair:
    if selection.binding != purchase.capture.binding or selection.revision != purchase.capture.revision:
        raise ValueError("PR repair capture is stale")
    component = selection.component
    if (component.gates or (component.watermarks.last_admitted > component.watermarks.last_applied
                           and component.watermarks.last_admitted_fence == selection.revision.fence)
            or getattr(component.state, purchase.allowance.value) >= purchase.ceiling
            or purchase.reply_at and not reply_advances(purchase.reply_at, component.state.consumed_reply_at)):
        raise ValueError("PR repair purchase is held, consumed or exhausted")
    state = replace(component.state, consumed_reply_at=purchase.reply_at) if purchase.reply_at else component.state
    return replace(component, state=state, watermarks=component.watermarks.admit(version, selection.revision.fence))


def consume_steward(selection: SelectedSteward, purchase: StewardPurchase, version: int) -> ActiveSteward:
    if not purchase.baseline_at:
        raise ValueError("steward purchase requires a nonempty input baseline")
    if selection.binding != purchase.capture.binding or selection.revision != purchase.capture.revision:
        raise ValueError("steward capture is stale")
    component = selection.component
    lane = component.state.triage if purchase.lane is StewardLane.TRIAGE else component.state.groom
    content = component.state.groom_content
    if purchase.lane is StewardLane.GROOM and (
            not purchase.content_signature or purchase.content_signature == content.successful
            or lane.baseline_at and not reply_advances(purchase.baseline_at, lane.baseline_at)
            or lane.attempted_at and (reply_advances(lane.attempted_at, purchase.baseline_at)
                or purchase.content_signature != content.attempted and not reply_advances(purchase.baseline_at, lane.attempted_at))):
        raise ValueError("groom input is absent, already delivered or older than its selected attempt")
    newer_input = (bool(content.attempted) and purchase.content_signature != content.attempted
                   if purchase.lane is StewardLane.GROOM else bool(lane.attempted_at) and reply_advances(purchase.baseline_at, lane.attempted_at))
    if (component.gates or (component.watermarks.last_admitted > component.watermarks.last_applied
                           and component.watermarks.last_admitted_fence == selection.revision.fence)
            or component.state.triage.pending_delivery or component.state.groom.pending_delivery
            or (lane.stalls >= purchase.stall_ceiling and not newer_input)
            or purchase.lane is StewardLane.TRIAGE and not reply_advances(purchase.baseline_at, lane.baseline_at)):
        raise ValueError("steward purchase is held, pending delivery or exhausted")
    lane = replace(lane, attempted_at=purchase.baseline_at, stalls=0 if newer_input else lane.stalls)
    state = replace(component.state, **{purchase.lane.value: lane})
    if purchase.lane is StewardLane.GROOM:
        state = replace(state, groom_content=replace(content, attempted=purchase.content_signature))
    component = replace(component, state=state)
    return replace(component, watermarks=component.watermarks.admit(version, selection.revision.fence))
