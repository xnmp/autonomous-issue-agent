"""Fixed secondary continuity values; the target aggregate owns the shared fence."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from .agent_task import RuntimeName
from .refs import issue_from_branch
from .contracts import CommandId, EffectReceipt, EffectStatus, ContinuationAdmission, SettlementRequest, WorkflowAttemptIdentity
from .workflow_identity import (
    MAX_WORKFLOW_GATES, ContinuationComponentIdentity, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowRevision, WorkflowFence,
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
class RepairModel:
    source: ContinuationAdmission
    runtime: RuntimeName
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, ContinuationAdmission) or self.source.attempt.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise ValueError("repair model requires its PR repair admission")
        if not isinstance(self.runtime, RuntimeName):
            raise ValueError("repair model requires a typed runtime")
        validate_workflow_text(self.model, "actual repair model")


@dataclass(frozen=True)
class ReviewRetryState:
    review_at: str = ""
    count: int = 0
    pr: PullRequestRef | None = None
    pending: ContinuationAdmission | None = None

    def __post_init__(self) -> None:
        _timestamp(self.review_at, "qualifying review timestamp")
        validate_workflow_integer(self.count, "review retry count")
        if self.pr is not None and not isinstance(self.pr, PullRequestRef):
            raise ValueError("review retry requires typed PR linkage")
        if self.pending is not None and (not self.review_at or self.pr is None
                or not isinstance(self.pending, ContinuationAdmission)
                or self.pending.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY):
            raise ValueError("pending review requires its selected issue admission and episode")


class FixInfraOutcome(str, Enum):
    """The one recorded fix-run end. An ended run without it is a machine end."""

    RETURNED_NORMAL = "returned_normal"


@dataclass(frozen=True)
class FixInfraReceipt:
    """The per-run machine classification the backoff reads (domain.backoff)."""

    admission: ContinuationAdmission
    outcome: FixInfraOutcome

    def __post_init__(self) -> None:
        if (not isinstance(self.admission, ContinuationAdmission)
                or self.admission.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY
                or not isinstance(self.outcome, FixInfraOutcome)):
            raise ValueError("fix infrastructure receipt requires its exact run classification")


@dataclass(frozen=True)
class IssueRecoveryState:
    consumed_reply_at: str = ""
    resumes: int = 0
    remembered_branch: str = ""
    ci_failures: int = 0
    review_failures: ReviewRetryState = field(default_factory=ReviewRetryState)
    rebase_failures: int = 0
    pending_ci: ContinuationAdmission | None = field(default=None, repr=False)
    pending_rebase: ContinuationAdmission | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _timestamp(self.consumed_reply_at, "consumed issue reply")
        validate_workflow_text(self.remembered_branch, "remembered issue branch", empty=True)
        for name in ("resumes", "ci_failures", "rebase_failures"):
            validate_workflow_integer(getattr(self, name), name)
        if not isinstance(self.review_failures, ReviewRetryState):
            raise ValueError("review retry allowance requires one typed timestamp/count state")
        if self.pending_ci is not None and (not isinstance(self.pending_ci, ContinuationAdmission)
                or self.pending_ci.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY):
            raise ValueError("pending CI requires its exact issue recovery admission")
        if self.pending_rebase is not None and (not isinstance(self.pending_rebase, ContinuationAdmission)
                or self.pending_rebase.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY):
            raise ValueError("pending rebase requires its exact issue recovery admission")


@dataclass(frozen=True)
class PRBlankReplySource:
    binding: WorkflowBinding
    reply_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WorkflowBinding) or self.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise ValueError("blank PR reply source requires its exact PR repair binding")
        _timestamp(self.reply_at, "blank PR reply source")
        if not self.reply_at:
            raise ValueError("blank PR reply source requires its selected timestamp")


@dataclass(frozen=True)
class CIRemediationCapture:
    binding: WorkflowBinding
    head: str
    run_ids: tuple[int, ...]
    decided_at: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WorkflowBinding) or self.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise ValueError("CI remediation requires its selected PR repair binding")
        validate_workflow_identifier(self.head, "CI remediation head")
        if (not isinstance(self.run_ids, tuple) or not self.run_ids or len(self.run_ids) > 64
                or any(type(run) is not int or run <= 0 or run > 2**63-1 for run in self.run_ids)
                or tuple(sorted(set(self.run_ids))) != self.run_ids):
            raise ValueError("CI remediation requires bounded exact ordered workflow runs")
        _timestamp(self.decided_at, "CI cancellation decision")


@dataclass(frozen=True)
class OwedCIRemediation:
    command_id: CommandId
    capture: CIRemediationCapture

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "CI remediation command identity")
        if not isinstance(self.capture, CIRemediationCapture):
            raise ValueError("owed CI remediation requires its exact command and capture")


@dataclass(frozen=True)
class SettledCIRemediation:
    owed: OwedCIRemediation
    receipt: EffectReceipt

    def __post_init__(self) -> None:
        if (not isinstance(self.owed, OwedCIRemediation) or not isinstance(self.receipt, EffectReceipt)
                or self.receipt.command_id != self.owed.command_id
                or self.receipt.status not in {EffectStatus.SUCCEEDED, EffectStatus.PERMANENT_FAILURE}):
            raise ValueError("settled CI remediation requires its exact verified terminal receipt")


CIRemediation = OwedCIRemediation | SettledCIRemediation


@dataclass(frozen=True)
class BranchRefreshCapture:
    binding: WorkflowBinding
    head: str
    base: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WorkflowBinding) or self.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise ValueError("branch refresh requires selected PR repair authority")
        validate_workflow_identifier(self.head, "branch refresh head")
        validate_workflow_text(self.base, "branch refresh base", empty=True)


@dataclass(frozen=True)
class PendingCIEscalationCapture:
    binding: WorkflowBinding
    head: str
    decision_head: str
    body: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WorkflowBinding) or self.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise ValueError("pending CI escalation requires selected PR repair authority")
        validate_workflow_identifier(self.head, "pending CI observed head")
        validate_workflow_text(self.decision_head, "pending CI decision head", empty=True)
        validate_workflow_text(self.body, "pending CI escalation body")
        if self.decision_head and self.decision_head != self.head:
            raise ValueError("pending CI decision differs from its observed head")


@dataclass(frozen=True)
class PublicationIdentityEscalationCapture:
    binding: WorkflowBinding
    head: str
    expected_branch: str
    issue: int
    checkout_branch: str
    body: str
    removed_label: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WorkflowBinding) or self.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise ValueError("publication escalation requires selected PR repair authority")
        validate_workflow_identifier(self.head, "publication head")
        validate_workflow_integer(self.issue, "publication linked issue", minimum=1)
        for name in ("expected_branch", "checkout_branch", "body", "removed_label"):
            validate_workflow_text(getattr(self, name), name)
        if self.expected_branch == self.checkout_branch or self.removed_label not in {"ai-pr-open", "ai-blocked"}:
            raise ValueError("publication escalation requires a branch mismatch and lifecycle label")


class PublicationIdentityMismatch(Exception):
    """Harness-captured pre-admission fault; provider statuses cannot mint it."""

    def __init__(self, capture: PublicationIdentityEscalationCapture):
        if not isinstance(capture, PublicationIdentityEscalationCapture):
            raise ValueError("publication refusal requires typed captured evidence")
        self.capture = capture
        super().__init__("repair checkout differs from the current remote branch")


PRRecoveryCapture = BranchRefreshCapture | PendingCIEscalationCapture | PublicationIdentityEscalationCapture


@dataclass(frozen=True)
class OwedPRRecoveryEffect:
    command_id: CommandId
    capture: PRRecoveryCapture

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "PR recovery command")
        if not isinstance(self.capture, (BranchRefreshCapture, PendingCIEscalationCapture, PublicationIdentityEscalationCapture)):
            raise ValueError("PR recovery effect requires fixed captured provenance")


@dataclass(frozen=True)
class SettledPRRecoveryEffect:
    owed: OwedPRRecoveryEffect
    receipt: EffectReceipt

    def __post_init__(self) -> None:
        if (not isinstance(self.owed, OwedPRRecoveryEffect) or not isinstance(self.receipt, EffectReceipt)
                or self.receipt.command_id != self.owed.command_id
                or self.receipt.status not in {EffectStatus.SUCCEEDED, EffectStatus.PERMANENT_FAILURE}):
            raise ValueError("PR recovery completion requires its exact terminal receipt")


@dataclass(frozen=True)
class PRRepairState:
    consumed_reply_at: str = ""
    branch: str = ""
    session: RepairSession | None = None
    ci_failures: int = 0
    review_failures: int = 0
    rebase_failures: int = 0
    infra_failures: int = 0
    resumes: int = field(default=0, repr=False)
    actual_model: RepairModel | None = field(default=None, repr=False)
    blank_reply: PRBlankReplySource | None = field(default=None, repr=False)
    ci_rerun: CIRemediation | None = field(default=None, repr=False)
    ci_cancel: CIRemediation | None = field(default=None, repr=False)
    branch_refresh: OwedPRRecoveryEffect | SettledPRRecoveryEffect | None = field(default=None, repr=False)
    pending_ci_escalation: OwedPRRecoveryEffect | SettledPRRecoveryEffect | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _timestamp(self.consumed_reply_at, "consumed PR reply")
        validate_workflow_text(self.branch, "repair branch", empty=True)
        if self.session is not None and not isinstance(self.session, RepairSession):
            raise ValueError("PR repair continuity requires a typed session")
        validate_workflow_integer(self.resumes, "repair resumes")
        for remediation in (self.ci_rerun, self.ci_cancel):
            if remediation is not None and not isinstance(remediation, (OwedCIRemediation, SettledCIRemediation)):
                raise ValueError("CI remediation requires fixed typed provenance")
        for remedy, captured in ((self.branch_refresh, BranchRefreshCapture),
                                 (self.pending_ci_escalation, (PendingCIEscalationCapture, PublicationIdentityEscalationCapture))):
            if remedy is None:
                continue
            owed = remedy.owed if isinstance(remedy, SettledPRRecoveryEffect) else remedy
            if not isinstance(owed, OwedPRRecoveryEffect) or not isinstance(owed.capture, captured):
                raise ValueError("PR recovery field differs from its fixed effect provenance")
        if self.actual_model is not None and not isinstance(self.actual_model, RepairModel):
            raise ValueError("actual repair model requires typed provenance")
        if self.blank_reply is not None and (not isinstance(self.blank_reply, PRBlankReplySource)
                or self.blank_reply.reply_at != self.consumed_reply_at):
            raise ValueError("blank PR source must name the selected consumed reply")
        for name in ("ci_failures", "review_failures", "rebase_failures", "infra_failures"):
            validate_workflow_integer(getattr(self, name), name)


def retire_pr_repair_continuity(state: PRRepairState, merged: bool) -> PRRepairState:
    if not isinstance(state, PRRepairState) or type(merged) is not bool:
        raise ValueError("repair retirement requires typed continuity and terminal outcome")
    # A closed-unmerged requeue clears only resume-blocking state. Remembered
    # branch/model, fork count and existing failure debt retain their old meaning.
    return PRRepairState() if merged else replace(state, session=None, consumed_reply_at="", blank_reply=None)


@dataclass(frozen=True)
class PRRepairCleanup:
    binding: WorkflowBinding
    revision: WorkflowRevision
    head: str
    merged: bool

    def __post_init__(self) -> None:
        if (not isinstance(self.binding, WorkflowBinding) or self.binding.authority.family is not WorkflowFamily.PR_REPAIR
                or not isinstance(self.revision, WorkflowRevision) or self.revision.fence != self.binding.fence
                or type(self.merged) is not bool):
            raise ValueError("PR cleanup requires its exact selected binding/revision/disposition")
        validate_workflow_identifier(self.head, "retired PR head")


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
class ActiveIssueRecovery:
    identity: ContinuationComponentIdentity
    state: IssueRecoveryState
    watermarks: ContinuationWatermarks = ContinuationWatermarks()
    gates: tuple[UnresolvedGate, ...] = ()

    def __post_init__(self) -> None:
        _active(self.identity, WorkflowFamily.ISSUE_RECOVERY, self.state, IssueRecoveryState, self.watermarks, self.gates)
        for pending in (self.state.pending_ci, self.state.review_failures.pending, self.state.pending_rebase):
            if pending is not None and (pending.attempt.binding.fence.key != self.identity.key
                    or pending.attempt.binding.authority != self.identity.authority
                    or pending.attempt.binding.episode_id != self.identity.episode_id
                    or pending.admission_version > self.watermarks.last_admitted):
                raise ValueError("pending repair differs from admitted issue component provenance")


@dataclass(frozen=True)
class InactivePRRepair:
    pass


@dataclass(frozen=True)
class ActivePRRepair:
    identity: ContinuationComponentIdentity
    state: PRRepairState
    watermarks: ContinuationWatermarks = ContinuationWatermarks()
    gates: tuple[UnresolvedGate, ...] = ()

    def __post_init__(self) -> None:
        _active(self.identity, WorkflowFamily.PR_REPAIR, self.state, PRRepairState, self.watermarks, self.gates)
        blank = self.state.blank_reply
        if blank is not None and (blank.binding.fence.key != self.identity.key
                or blank.binding.authority != self.identity.authority or blank.binding.episode_id != self.identity.episode_id):
            raise ValueError("blank PR receipt belongs to another component")
        for selected in (self.state.session, self.state.actual_model):
            if selected is None:
                continue
            source = selected.source
            prior = source.attempt.binding
            if (prior.fence.key != self.identity.key or prior.authority != self.identity.authority
                    or prior.episode_id != self.identity.episode_id
                    or source.admission_version > self.watermarks.last_applied):
                raise ValueError("repair session disagrees with applied component provenance")


IssueRecoveryComponent = InactiveIssueRecovery | ActiveIssueRecovery
PRRepairComponent = InactivePRRepair | ActivePRRepair

def validate_component_revision(component: object, revision: WorkflowRevision, family: WorkflowFamily) -> None:
    types: tuple[type, ...]
    if family is WorkflowFamily.ISSUE_RECOVERY:
        types = (InactiveIssueRecovery, ActiveIssueRecovery)
    elif family is WorkflowFamily.PR_REPAIR:
        types = (InactivePRRepair, ActivePRRepair)
    else:
        raise ValueError("continuation family must be one of the fixed components")
    if not isinstance(revision, WorkflowRevision) or not isinstance(component, types):
        raise ValueError("aggregate requires its fixed typed continuation component")
    if isinstance(component, (ActiveIssueRecovery, ActivePRRepair)) and (
        component.identity.key != revision.fence.key
        or component.watermarks.last_admitted > revision.version
    ):
        raise ValueError("component cannot alias a target or originate in a future revision")

    if isinstance(component, (ActiveIssueRecovery, ActivePRRepair)):
        admitted = component.watermarks.last_admitted_fence
        if admitted is not None and (admitted.key != revision.fence.key or admitted.generation > revision.fence.generation):
            raise ValueError("component admission fence belongs to another target or future generation")
        from .workflow import RearmingGate
        if any((gate.gate if isinstance(gate, RearmingGate) else gate).opened_generation > revision.fence.generation
               for gate in component.gates):
            raise ValueError("component hold cannot originate in a future target generation")
    if isinstance(component, ActivePRRepair):
        if component.state.blank_reply is not None and component.state.blank_reply.binding.fence.generation > revision.fence.generation:
            raise ValueError("blank PR reply cannot originate in a future target generation")
        for selected in (component.state.session, component.state.actual_model):
            if selected is not None and selected.source.attempt.binding.fence.generation > revision.fence.generation:
                raise ValueError("repair continuity cannot originate in a future target generation")
    if isinstance(component, ActiveIssueRecovery) and component.state.pending_ci is not None:
        if component.state.pending_ci.attempt.binding.fence.generation > revision.fence.generation:
            raise ValueError("pending CI cannot originate in a future target generation")
    if isinstance(component, ActiveIssueRecovery) and component.state.review_failures.pending is not None:
        if component.state.review_failures.pending.attempt.binding.fence.generation > revision.fence.generation:
            raise ValueError("pending review cannot originate in a future target generation")
    if isinstance(component, ActiveIssueRecovery) and component.state.pending_rebase is not None:
        if component.state.pending_rebase.attempt.binding.fence.generation > revision.fence.generation:
            raise ValueError("pending rebase cannot originate in a future target generation")


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


IssueRecoverySelection = InactiveIssueRecovery | SelectedIssueRecovery
PRRepairSelection = InactivePRRepair | SelectedPRRepair

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


def continuation_return_is_current(selection: SelectedIssueRecovery | SelectedPRRepair,
                                   admission: ContinuationAdmission) -> bool:
    if not isinstance(selection, (SelectedIssueRecovery, SelectedPRRepair)) or not isinstance(admission, ContinuationAdmission):
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
            WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR,
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


@dataclass(frozen=True)
class IssueInfraPurchase:
    capture: ContinuationCapture

    def __post_init__(self) -> None:
        if not isinstance(self.capture, ContinuationCapture) or self.capture.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY:
            raise ValueError("fix infrastructure observation requires its issue family capture")


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
class IssueCIPurchase:
    capture: ContinuationCapture
    pr: PullRequestRef
    head: str
    ceiling: int

    def __post_init__(self) -> None:
        if (not isinstance(self.capture, ContinuationCapture)
                or self.capture.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY
                or not isinstance(self.pr, PullRequestRef)):
            raise ValueError("CI purchase requires selected issue recovery and linked PR")
        validate_workflow_text(self.head, "observed CI head")
        validate_workflow_integer(self.ceiling, "CI allowance ceiling", minimum=1)


@dataclass(frozen=True)
class CIRepairInput:
    selection: SelectedIssueRecovery
    pr: PullRequestRef
    head: str
    ceiling: int

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectedIssueRecovery):
            raise ValueError("CI input requires selected issue recovery")
        IssueCIPurchase(ContinuationCapture(self.selection.binding, self.selection.revision, "validation"),
                        self.pr, self.head, self.ceiling)


class CICharge(str, Enum):
    ATTEMPT = "attempt"
    NOT_ATTEMPT = "not_attempt"
    NOT_LAUNCHED = "not_launched"


@dataclass(frozen=True)
class CIRepairResult:
    admission: ContinuationAdmission
    charge: CICharge

    def __post_init__(self) -> None:
        if (not isinstance(self.admission, ContinuationAdmission)
                or self.admission.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY
                or not isinstance(self.charge, CICharge)):
            raise ValueError("CI result requires its exact issue admission and classification")


@dataclass(frozen=True)
class IssueReviewPurchase:
    capture: ContinuationCapture
    pr: PullRequestRef
    head: str
    review_at: str
    ceiling: int

    def __post_init__(self) -> None:
        IssueCIPurchase(self.capture, self.pr, self.head, self.ceiling)
        _timestamp(self.review_at, "selected qualifying review")
        if not self.review_at:
            raise ValueError("review repair requires a qualifying review timestamp")


@dataclass(frozen=True)
class ReviewRepairInput:
    selection: SelectedIssueRecovery
    pr: PullRequestRef
    head: str
    review_at: str
    ceiling: int

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectedIssueRecovery):
            raise ValueError("review input requires selected issue recovery")
        IssueReviewPurchase(ContinuationCapture(self.selection.binding, self.selection.revision, "validation"),
                            self.pr, self.head, self.review_at, self.ceiling)


class ReviewCharge(str, Enum):
    ATTEMPT = "attempt"
    NOT_ATTEMPT = "not_attempt"
    NOT_LAUNCHED = "not_launched"


@dataclass(frozen=True)
class ReviewRepairResult:
    admission: ContinuationAdmission
    charge: ReviewCharge

    def __post_init__(self) -> None:
        if (not isinstance(self.admission, ContinuationAdmission)
                or self.admission.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY
                or not isinstance(self.charge, ReviewCharge)):
            raise ValueError("review result requires its exact issue admission and classification")


@dataclass(frozen=True)
class CIEpisodeReset:
    binding: WorkflowBinding
    revision: WorkflowRevision
    pr_fence: WorkflowFence
    head: str

    def __post_init__(self) -> None:
        if (not isinstance(self.binding, WorkflowBinding)
                or self.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY
                or not isinstance(self.revision, WorkflowRevision) or self.revision.fence != self.binding.fence
                or not isinstance(self.pr_fence, WorkflowFence) or not isinstance(self.pr_fence.key.target, PullRequestRef)
                or self.pr_fence.key.repository != self.binding.fence.key.repository):
            raise ValueError("CI reset requires exact issue authority and observed PR fence")
        validate_workflow_text(self.head, "settled green CI head")


@dataclass(frozen=True)
class IssueRebasePurchase:
    capture: ContinuationCapture
    pr: PullRequestRef
    head: str
    branch: str
    ceiling: int

    def __post_init__(self) -> None:
        IssueCIPurchase(self.capture, self.pr, self.head, self.ceiling)
        validate_workflow_text(self.branch, "selected rebase branch")


@dataclass(frozen=True)
class RebaseRepairInput:
    selection: SelectedIssueRecovery
    pr: PullRequestRef
    head: str
    branch: str
    ceiling: int

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectedIssueRecovery):
            raise ValueError("rebase input requires selected issue recovery")
        IssueRebasePurchase(ContinuationCapture(self.selection.binding, self.selection.revision, "validation"),
                            self.pr, self.head, self.branch, self.ceiling)


class RebaseCharge(str, Enum):
    ATTEMPT = "attempt"
    NOT_ATTEMPT = "not_attempt"
    NOT_LAUNCHED = "not_launched"


@dataclass(frozen=True)
class RebaseRepairResult:
    admission: ContinuationAdmission
    charge: RebaseCharge

    def __post_init__(self) -> None:
        if (not isinstance(self.admission, ContinuationAdmission)
                or self.admission.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY
                or not isinstance(self.charge, RebaseCharge)):
            raise ValueError("rebase result requires its exact issue admission and classification")


@dataclass(frozen=True)
class PRRepairPurchase:
    capture: ContinuationCapture
    reply_at: str = ""
    allowance: RepairAllowance = RepairAllowance.INFRA
    ceiling: int = 1
    branch: str = ""
    handoff: RepairHandoff | None = None
    advance_resume: bool = False
    head: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.capture, ContinuationCapture) or self.capture.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise ValueError("PR repair purchase requires its family capture")
        validate_workflow_text(self.branch, "purchased repair branch", empty=True)
        validate_workflow_text(self.head, "purchased repair head", limit=256, empty=True)
        if self.handoff is not None and (not isinstance(self.handoff, RepairHandoff) or self.handoff.branch != self.branch
                or self.handoff.pr != self.capture.binding.fence.key.target):
            raise ValueError("purchased repair handoff differs from its PR/branch")
        _timestamp(self.reply_at, "selected PR reply")
        if not isinstance(self.allowance, RepairAllowance):
            raise ValueError("PR repair purchase requires a fixed allowance")
        validate_workflow_integer(self.ceiling, "repair allowance ceiling", minimum=1)
        if type(self.advance_resume) is not bool:
            raise ValueError("repair resume purchase requires an exact boolean")


@dataclass(frozen=True)
class RepairHandoff:
    source: WorkflowAttemptIdentity
    handle: str
    runtime: RuntimeName
    model: str
    branch: str
    pr: PullRequestRef
    resumes: int

    def __post_init__(self) -> None:
        if not isinstance(self.source, WorkflowAttemptIdentity) or self.source.binding.authority.family is not WorkflowFamily.RESOLVE:
            raise ValueError("repair handoff requires its exact resolve source")
        if not isinstance(self.pr, PullRequestRef):
            raise ValueError("repair handoff requires its exact destination PR")
        validate_workflow_integer(self.resumes, "handed off resume count")
        validate_workflow_identifier(self.handle, "resolve handoff session")
        validate_workflow_text(self.model, "resolve handoff model", empty=True)
        validate_workflow_text(self.branch, "resolve handoff branch")
        if not isinstance(self.runtime, RuntimeName):
            raise ValueError("resolve handoff requires typed runtime")


@dataclass(frozen=True)
class PRReplyInput:
    reply_at: str
    body: str

    def __post_init__(self) -> None:
        _timestamp(self.reply_at, "selected PR reply")
        validate_workflow_text(self.body, "selected PR reply body", limit=65536)
        if not self.reply_at or not self.body.strip():
            raise ValueError("paid PR reply requires its exact timestamp and nonblank content")


@dataclass(frozen=True)
class PRRepairInput:
    selection: SelectedPRRepair
    branch: str
    runtime: RuntimeName
    handoff: RepairHandoff | None = None
    ci: CIRepairInput | None = None
    review: ReviewRepairInput | None = None
    rebase: RebaseRepairInput | None = None
    reply: PRReplyInput | None = None
    head: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectedPRRepair) or not isinstance(self.runtime, RuntimeName):
            raise ValueError("repair input requires selected authority and runtime")
        validate_workflow_text(self.branch, "current remote repair branch")
        validate_workflow_text(self.head, "current remote repair head", limit=256, empty=True)
        if self.reply is not None and (self.ci is not None or self.review is not None or self.rebase is not None
                or not isinstance(self.reply, PRReplyInput)
                or not reply_advances(self.reply.reply_at, self.selection.component.state.consumed_reply_at)):
            raise ValueError("PR reply is consumed, stale or mixed with another repair lane")
        if self.rebase is not None and (self.ci is not None or self.review is not None
                or not isinstance(self.rebase, RebaseRepairInput)
                or self.rebase.pr != self.selection.binding.fence.key.target or self.rebase.branch != self.branch
                or self.rebase.selection.binding.fence.key.repository != self.selection.binding.fence.key.repository):
            raise ValueError("rebase input differs from selected repair PR, branch or lane")
        if self.review is not None and (self.ci is not None or not isinstance(self.review, ReviewRepairInput)
                or self.review.pr != self.selection.binding.fence.key.target
                or self.review.selection.binding.fence.key.repository != self.selection.binding.fence.key.repository):
            raise ValueError("review input differs from selected repair PR/repository or lane")
        if self.ci is not None and (not isinstance(self.ci, CIRepairInput)
                or self.ci.pr != self.selection.binding.fence.key.target
                or self.ci.selection.binding.fence.key.repository != self.selection.binding.fence.key.repository):
            raise ValueError("CI input differs from selected repair PR/repository")
        if self.handoff is not None and (not isinstance(self.handoff, RepairHandoff) or self.handoff.branch != self.branch
                or self.handoff.runtime is not self.runtime or self.handoff.pr != self.selection.binding.fence.key.target):
            raise ValueError("repair handoff differs from selected branch/runtime")

    @property
    def session(self) -> RepairSession | RepairHandoff | None:
        state = self.selection.component.state
        session = state.session
        if session is not None and session.runtime is self.runtime and state.branch == self.branch:
            return session
        return self.handoff if state.actual_model is None else None

    @property
    def model(self) -> str:
        state = self.selection.component.state
        model = state.actual_model
        if model is not None and model.runtime is self.runtime:
            return model.model
        return self.session.model if self.session is not None else ""


@dataclass(frozen=True)
class PRRepairNotLaunched:
    admission: ContinuationAdmission

    def __post_init__(self) -> None:
        if not isinstance(self.admission, ContinuationAdmission) or self.admission.attempt.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise ValueError("unlaunched repair requires its exact admission")


@dataclass(frozen=True)
class PRRepairUnreturned:
    """A launched repair proven terminal and quiescent with no captured return.

    It acknowledges the admission only; it carries no output, session, model or
    settlement, so it cannot be read back as a normal paid return.
    """
    admission: ContinuationAdmission

    def __post_init__(self) -> None:
        if not isinstance(self.admission, ContinuationAdmission) or self.admission.attempt.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise ValueError("unreturned repair requires its exact admission")


@dataclass(frozen=True)
class PRRepairResult:
    admission: ContinuationAdmission
    state: PRRepairState
    settlement: SettlementRequest
    ci: CIRepairResult | None = None
    review: ReviewRepairResult | None = None
    rebase: RebaseRepairResult | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.admission, ContinuationAdmission)
                or self.admission.attempt.binding.authority.family is not WorkflowFamily.PR_REPAIR
                or not isinstance(self.state, PRRepairState) or not isinstance(self.settlement, SettlementRequest)
                or self.settlement.run_id != self.admission.attempt.run_id):
            raise ValueError("repair result requires its exact paid admission and settlement")
        if self.ci is not None and (not isinstance(self.ci, CIRepairResult)
                or self.ci.admission.attempt.run_id != self.admission.attempt.run_id
                or self.ci.admission.attempt.leg_id != self.admission.attempt.leg_id
                or self.ci.admission.attempt.binding.fence.key.repository != self.admission.attempt.binding.fence.key.repository
                or self.ci.charge is CICharge.NOT_LAUNCHED):
            raise ValueError("paid CI result requires the same launched run and leg")
        if self.review is not None and (self.ci is not None or not isinstance(self.review, ReviewRepairResult)
                or self.review.admission.attempt.run_id != self.admission.attempt.run_id
                or self.review.admission.attempt.leg_id != self.admission.attempt.leg_id
                or self.review.admission.attempt.binding.fence.key.repository != self.admission.attempt.binding.fence.key.repository
                or self.review.charge is ReviewCharge.NOT_LAUNCHED):
            raise ValueError("paid review result requires the same launched run and leg")
        if (self.state.session is not None and self.state.session.source != self.admission
                or self.state.actual_model is None or self.state.actual_model.source != self.admission):
            raise ValueError("repair output requires its exact admitted provenance")
        if self.rebase is not None and (self.ci is not None or self.review is not None
                or not isinstance(self.rebase, RebaseRepairResult)
                or self.rebase.admission.attempt.run_id != self.admission.attempt.run_id
                or self.rebase.admission.attempt.leg_id != self.admission.attempt.leg_id
                or self.rebase.admission.attempt.binding.fence.key.repository != self.admission.attempt.binding.fence.key.repository
                or self.rebase.charge is RebaseCharge.NOT_LAUNCHED):
            raise ValueError("paid rebase result requires the same launched run and leg")
        if self.state.session is not None and (
                self.state.session.runtime is not self.state.actual_model.runtime
                or self.state.session.model != self.state.actual_model.model):
            raise ValueError("repair handle must come from the actual returned runtime and model")


ContinuationPurchase = IssueInfraPurchase | IssueRecoveryPurchase | IssueReplyPurchase | IssueCIPurchase | IssueReviewPurchase | IssueRebasePurchase | PRRepairPurchase


def consume_issue_rebase(selection: SelectedIssueRecovery, purchase: IssueRebasePurchase, admission: ContinuationAdmission) -> ActiveIssueRecovery:
    if selection.binding != purchase.capture.binding or selection.revision != purchase.capture.revision:
        raise ValueError("rebase issue capture is stale")
    component = selection.component
    if (admission.attempt.binding != selection.binding or admission.admission_version != selection.revision.version + 1
            or admission.attempt.leg_id != purchase.capture.leg_id):
        raise ValueError("rebase admission differs from selected authority")
    pending = component.state.pending_rebase
    if (component.gates or component.state.rebase_failures >= purchase.ceiling
            or pending is not None and pending.attempt.binding == selection.binding):
        raise ValueError("rebase allowance is held, pending or exhausted")
    return replace(component, state=replace(component.state, pending_rebase=admission),
                   watermarks=component.watermarks.admit(admission.admission_version, selection.revision.fence))


def rebase_return_is_current(selection: SelectedIssueRecovery, admission: ContinuationAdmission) -> bool:
    return selection.binding == admission.attempt.binding and selection.component.state.pending_rebase == admission


def consume_issue_review(selection: SelectedIssueRecovery, purchase: IssueReviewPurchase, admission: ContinuationAdmission) -> ActiveIssueRecovery:
    if selection.binding != purchase.capture.binding or selection.revision != purchase.capture.revision:
        raise ValueError("review issue capture is stale")
    component = selection.component
    debt = component.state.review_failures
    if (admission.attempt.binding != selection.binding or admission.admission_version != selection.revision.version + 1
            or admission.attempt.leg_id != purchase.capture.leg_id):
        raise ValueError("review admission differs from its selected authority")
    newer = reply_advances(purchase.review_at, debt.review_at)
    if (component.gates or debt.count and not debt.review_at
            or debt.pr is not None and debt.pr != purchase.pr
            or debt.pending is not None and debt.pending.attempt.binding == selection.binding
            or debt.review_at and purchase.review_at != debt.review_at and not newer
            or not newer and debt.count >= purchase.ceiling):
        raise ValueError("review allowance is held, pending, exhausted, relinked or older")
    debt = ReviewRetryState(purchase.review_at, 0 if newer else debt.count, purchase.pr, admission)
    return replace(component, state=replace(component.state, review_failures=debt),
                   watermarks=component.watermarks.admit(admission.admission_version, selection.revision.fence))


def review_return_is_current(selection: SelectedIssueRecovery, admission: ContinuationAdmission) -> bool:
    return selection.binding == admission.attempt.binding and selection.component.state.review_failures.pending == admission


def consume_issue_ci(selection: SelectedIssueRecovery, purchase: IssueCIPurchase, admission: ContinuationAdmission) -> ActiveIssueRecovery:
    if selection.binding != purchase.capture.binding or selection.revision != purchase.capture.revision:
        raise ValueError("CI issue capture is stale")
    component = selection.component
    if (admission.attempt.binding != selection.binding or admission.admission_version != selection.revision.version + 1
            or admission.attempt.leg_id != purchase.capture.leg_id):
        raise ValueError("CI admission differs from its selected authority")
    pending = component.state.pending_ci
    if (component.gates or component.state.ci_failures >= purchase.ceiling
            or pending is not None and pending.attempt.binding == selection.binding):
        raise ValueError("CI issue allowance is held, pending or exhausted")
    return replace(component, state=replace(component.state, pending_ci=admission),
                   watermarks=component.watermarks.admit(admission.admission_version, selection.revision.fence))


def ci_return_is_current(selection: SelectedIssueRecovery, admission: ContinuationAdmission) -> bool:
    return selection.binding == admission.attempt.binding and selection.component.state.pending_ci == admission


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
    state = replace(component.state, consumed_reply_at=purchase.reply_at, blank_reply=None) if purchase.reply_at else component.state
    if purchase.handoff is not None:
        state = replace(state, resumes=max(state.resumes, purchase.handoff.resumes))
    if purchase.advance_resume:
        state = replace(state, resumes=state.resumes + 1)
    return replace(component, state=state, watermarks=component.watermarks.admit(version, selection.revision.fence))
