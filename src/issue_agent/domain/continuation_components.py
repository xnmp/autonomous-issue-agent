"""Fixed secondary continuity values; the target aggregate owns the shared fence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from .agent_task import RuntimeName
from .contracts import ContinuationAdmission
from .workflow_identity import (
    MAX_WORKFLOW_GATES, ContinuationComponentIdentity, FamilyAuthority, WorkflowBinding, WorkflowFamily, WorkflowKey, WorkflowRevision, WorkflowFence,
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


@dataclass(frozen=True)
class StewardLaneState:
    baseline_at: str = ""
    stalls: int = 0
    infra_failures: int = 0
    pending_delivery: tuple[str, ...] = ()

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


@dataclass(frozen=True)
class StewardState:
    triage: StewardLaneState = StewardLaneState()
    groom: StewardLaneState = StewardLaneState()

    def __post_init__(self) -> None:
        if not isinstance(self.triage, StewardLaneState) or not isinstance(self.groom, StewardLaneState):
            raise ValueError("steward continuity requires fixed triage and groom lanes")


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


class StewardLane(str, Enum):
    TRIAGE = "triage"
    GROOM = "groom"


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

    def __post_init__(self) -> None:
        if not isinstance(self.capture, ContinuationCapture) or self.capture.binding.authority.family is not WorkflowFamily.STEWARD:
            raise ValueError("steward purchase requires its family capture")
        if not isinstance(self.lane, StewardLane):
            raise ValueError("steward purchase requires a fixed lane")
        _timestamp(self.baseline_at, "selected steward baseline")
        validate_workflow_integer(self.stall_ceiling, "steward stall ceiling", minimum=1)


ContinuationPurchase = IssueRecoveryPurchase | PRRepairPurchase | StewardPurchase


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
    if selection.binding != purchase.capture.binding or selection.revision != purchase.capture.revision:
        raise ValueError("steward capture is stale")
    component = selection.component
    lane = component.state.triage if purchase.lane is StewardLane.TRIAGE else component.state.groom
    if (component.gates or (component.watermarks.last_admitted > component.watermarks.last_applied
                           and component.watermarks.last_admitted_fence == selection.revision.fence)
            or lane.pending_delivery or lane.stalls >= purchase.stall_ceiling):
        raise ValueError("steward purchase is held, pending delivery or exhausted")
    return replace(component, watermarks=component.watermarks.admit(version, selection.revision.fence))
