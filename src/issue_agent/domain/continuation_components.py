"""Fixed secondary continuity values; the target aggregate owns the shared fence."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .agent_task import RuntimeName
from .refs import issue_from_branch
from .contracts import BudgetScope, CommandId, EffectReceipt, EffectStatus, ContinuationAdmission, SettlementRequest, WorkflowAttemptIdentity
from .eligibility import IssueCompletionGuard, LifecycleOperation
from .self_clear import BudgetBlock, MachineBlock
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
    RETURNED_INFRA = "returned_infra"
    RETURNED_NORMAL = "returned_normal"
    RECLAIMED = "reclaimed"
    DETERMINISTIC_DEATH = "deterministic_death"


@dataclass(frozen=True)
class FixInfraEpisode:
    source: ContinuationAdmission
    latest_version: int
    reset_version: int

    def __post_init__(self) -> None:
        if (not isinstance(self.source, ContinuationAdmission)
                or self.source.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY):
            raise ValueError("fix infrastructure episode requires its exact issue admission")
        validate_workflow_integer(self.latest_version, "latest fix infrastructure input", minimum=1)
        validate_workflow_integer(self.reset_version, "fix infrastructure reset boundary")
        if self.source.admission_version > self.latest_version or self.reset_version > self.latest_version:
            raise ValueError("fix infrastructure episode has impossible ordering")


@dataclass(frozen=True)
class FixInfraReceipt:
    admission: ContinuationAdmission
    outcome: FixInfraOutcome
    failures: int
    episode: FixInfraEpisode | None
    charge: int = 0

    def __post_init__(self) -> None:
        if (not isinstance(self.admission, ContinuationAdmission)
                or self.admission.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY
                or not isinstance(self.outcome, FixInfraOutcome)):
            raise ValueError("fix infrastructure receipt requires its exact run classification")
        validate_workflow_integer(self.failures, "recorded fix infrastructure failures")
        validate_workflow_integer(self.charge, "attributed fix infrastructure charge")
        if self.episode is not None and self.charge > self.failures:
            raise ValueError("attributed infrastructure charge exceeds applied failures")
        if self.outcome is FixInfraOutcome.RETURNED_NORMAL and self.charge:
            raise ValueError("an actual non-infrastructure reply has no failure charge")
        if self.episode is not None and (not isinstance(self.episode, FixInfraEpisode)
                or self.episode.source != self.admission):
            raise ValueError("applied fix infrastructure receipt requires its exact source")


@dataclass(frozen=True)
class RecordedMachineBlock(MachineBlock):
    source: ContinuationAdmission

    def __post_init__(self) -> None:
        super().__post_init__()
        if (not isinstance(self.source, ContinuationAdmission)
                or self.source.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY
                or self.source.attempt.binding.fence.key.target.number != self.issue):
            raise ValueError("machine block requires its exact admitted issue source")


@dataclass(frozen=True, kw_only=True)
class RefusedBudgetBlock(BudgetBlock):
    capture: ContinuationCapture
    run_id: str
    reservation_id: str
    config_version: str
    budget_scope: BudgetScope
    request_fingerprint: str
    refused_at: datetime
    committed_micros: int

    def __post_init__(self) -> None:
        super().__post_init__()
        if (not isinstance(self.capture, ContinuationCapture)
                or self.capture.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY
                or self.capture.binding.fence.key.target.number != self.issue
                or not isinstance(self.budget_scope, BudgetScope)
                or not isinstance(self.budget_scope.period_timezone, ZoneInfo)
                or self.budget_scope.dimension != self.scope or self.budget_scope.currency != self.currency):
            raise ValueError("budget refusal requires its exact issue capture and applicable scope")
        if not ((self.budget_scope.repository == self.capture.binding.fence.key.repository
                 and self.scope in {"repository", f"issue:{self.issue}"})
                or self.budget_scope.repository == "*" and self.scope == "global"):
            raise ValueError("budget refusal has an unsupported or mismatched cap scope")
        for name in ("run_id", "reservation_id", "config_version", "request_fingerprint"):
            validate_workflow_identifier(getattr(self, name), "budget refusal " + name)
        validate_workflow_integer(self.committed_micros, "budget refusal committed money")
        if (self.committed_micros + self.reservation_micros <= self.cap_micros
                or self.old_headroom_micros != max(0, self.cap_micros-self.committed_micros)):
            raise ValueError("budget refusal requires its actual exhausted balance")
        if not isinstance(self.refused_at, datetime) or self.refused_at.tzinfo is None or self.refused_at.utcoffset() is None:
            raise ValueError("budget refusal requires its observed aware time")


@dataclass(frozen=True)
class IssueRecoveryState:
    consumed_reply_at: str = ""
    resumes: int = 0
    remembered_branch: str = ""
    ci_failures: int = 0
    review_failures: ReviewRetryState = field(default_factory=ReviewRetryState)
    rebase_failures: int = 0
    infra_failures: int = 0
    self_clears: int = 0
    pending_ci: ContinuationAdmission | None = field(default=None, repr=False)
    pending_rebase: ContinuationAdmission | None = field(default=None, repr=False)
    infra_episode: FixInfraEpisode | None = field(default=None, repr=False)
    machine_block: MachineBlock | None = field(default=None, repr=False)
    budget_block: BudgetBlock | None = field(default=None, repr=False)
    self_clear_ceiling_reported: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        _timestamp(self.consumed_reply_at, "consumed issue reply")
        validate_workflow_text(self.remembered_branch, "remembered issue branch", empty=True)
        for name in ("resumes", "ci_failures", "rebase_failures", "infra_failures", "self_clears"):
            validate_workflow_integer(getattr(self, name), name)
        if type(self.self_clear_ceiling_reported) is not bool or self.self_clear_ceiling_reported and self.self_clears < 3:
            raise ValueError("reported self-clear ceiling requires the shared exhausted allowance")
        if not isinstance(self.review_failures, ReviewRetryState):
            raise ValueError("review retry allowance requires one typed timestamp/count state")
        if self.pending_ci is not None and (not isinstance(self.pending_ci, ContinuationAdmission)
                or self.pending_ci.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY):
            raise ValueError("pending CI requires its exact issue recovery admission")
        if self.pending_rebase is not None and (not isinstance(self.pending_rebase, ContinuationAdmission)
                or self.pending_rebase.attempt.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY):
            raise ValueError("pending rebase requires its exact issue recovery admission")
        if self.infra_episode is not None and not isinstance(self.infra_episode, FixInfraEpisode):
            raise ValueError("fix infrastructure state requires one typed episode")
        if self.machine_block is not None and not isinstance(self.machine_block, MachineBlock):
            raise ValueError("machine hold requires its fixed typed cause and head")
        if self.budget_block is not None and not isinstance(self.budget_block, BudgetBlock):
            raise ValueError("budget hold requires its fixed typed reservation evidence")


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


@dataclass(frozen=True)
class ImportedCIRemediation:
    head: str
    decided_at: str
    import_identity: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.head, "imported CI remediation head")
        _timestamp(self.decided_at, "imported CI cancellation decision")
        validate_workflow_identifier(self.import_identity, "CI remediation import identity")


CIRemediation = OwedCIRemediation | SettledCIRemediation | ImportedCIRemediation


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
class ImportedBranchRefresh:
    head: str
    base: str
    import_identity: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.head, "imported branch refresh head")
        validate_workflow_text(self.base, "imported branch refresh base", empty=True)
        validate_workflow_identifier(self.import_identity, "branch refresh import identity")


@dataclass(frozen=True)
class ImportedPendingCIEscalation:
    head: str
    import_identity: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.head, "imported pending CI head")
        validate_workflow_identifier(self.import_identity, "pending CI import identity")


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
    resumes: int = field(default=0, repr=False)
    actual_model: RepairModel | None = field(default=None, repr=False)
    blank_reply: PRBlankReplySource | None = field(default=None, repr=False)
    ci_rerun: CIRemediation | None = field(default=None, repr=False)
    ci_cancel: CIRemediation | None = field(default=None, repr=False)
    branch_refresh: OwedPRRecoveryEffect | SettledPRRecoveryEffect | ImportedBranchRefresh | None = field(default=None, repr=False)
    pending_ci_escalation: OwedPRRecoveryEffect | SettledPRRecoveryEffect | ImportedPendingCIEscalation | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _timestamp(self.consumed_reply_at, "consumed PR reply")
        validate_workflow_text(self.branch, "repair branch", empty=True)
        if self.session is not None and not isinstance(self.session, RepairSession):
            raise ValueError("PR repair continuity requires a typed session")
        validate_workflow_integer(self.resumes, "repair resumes")
        for remediation in (self.ci_rerun, self.ci_cancel):
            if remediation is not None and not isinstance(remediation, (OwedCIRemediation, SettledCIRemediation, ImportedCIRemediation)):
                raise ValueError("CI remediation requires fixed typed provenance")
        for remedy, captured, imported in ((self.branch_refresh, BranchRefreshCapture, ImportedBranchRefresh),
                                           (self.pending_ci_escalation, (PendingCIEscalationCapture, PublicationIdentityEscalationCapture), ImportedPendingCIEscalation)):
            if remedy is None or isinstance(remedy, imported):
                continue
            owed = remedy.owed if isinstance(remedy, SettledPRRecoveryEffect) else remedy
            if not isinstance(owed, OwedPRRecoveryEffect) or not isinstance(owed.capture, captured):
                raise ValueError("PR recovery field differs from its fixed effect provenance")
        if self.actual_model is not None and not isinstance(self.actual_model, RepairModel):
            raise ValueError("actual repair model requires typed provenance")
        if self.blank_reply is not None and (not isinstance(self.blank_reply, PRBlankReplySource)
                or self.blank_reply.reply_at != self.consumed_reply_at):
            raise ValueError("blank PR source must name the selected consumed reply")
        for name in ("ci_failures", "review_failures", "rebase_failures", "infra_failures", "self_clears"):
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
        for pending in (self.state.pending_ci, self.state.review_failures.pending, self.state.pending_rebase):
            if pending is not None and (pending.attempt.binding.fence.key != self.identity.key
                    or pending.attempt.binding.authority != self.identity.authority
                    or pending.attempt.binding.episode_id != self.identity.episode_id
                    or pending.admission_version > self.watermarks.last_admitted):
                raise ValueError("pending repair differs from admitted issue component provenance")
        if self.state.infra_episode is not None:
            binding = self.state.infra_episode.source.attempt.binding
            if (binding.fence.key != self.identity.key or binding.authority != self.identity.authority
                    or binding.episode_id != self.identity.episode_id):
                raise ValueError("fix infrastructure episode differs from its issue component")
        block = self.state.machine_block
        if block is not None:
            if block.issue != self.identity.key.target.number:
                raise ValueError("machine hold belongs to another issue")
            if isinstance(block, RecordedMachineBlock):
                binding = block.source.attempt.binding
                if (binding.fence.key != self.identity.key or binding.authority != self.identity.authority
                        or binding.episode_id != self.identity.episode_id):
                    raise ValueError("machine hold differs from its issue component")
        budget = self.state.budget_block
        if budget is not None:
            if budget.issue != self.identity.key.target.number:
                raise ValueError("budget hold belongs to another issue")
            if isinstance(budget, RefusedBudgetBlock):
                binding = budget.capture.binding
                if (binding.fence.key != self.identity.key or binding.authority != self.identity.authority
                        or binding.episode_id != self.identity.episode_id):
                    raise ValueError("budget hold differs from its issue component")


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
    if isinstance(component, ActiveIssueRecovery) and component.state.infra_episode is not None:
        episode = component.state.infra_episode
        fence = episode.source.attempt.binding.fence
        if fence.generation > revision.fence.generation or fence == revision.fence and episode.latest_version > revision.version:
            raise ValueError("fix infrastructure episode cannot originate in a future target revision")


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

    @property
    def episode_scope(self) -> str:
        """Names this recovery episode and re-arm generation; a counter kept
        outside the component (pre-admission refusals, #262) is valid only
        while this value is unchanged."""
        return f"{self.component.identity.episode_id}@{self.revision.fence.generation}"


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
    fix_infra_ceiling: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.fix_infra_ceiling is not None:
            validate_workflow_integer(self.fix_infra_ceiling, "captured fix infrastructure ceiling")
        if not isinstance(self.binding, WorkflowBinding) or self.binding.authority.family not in (
            WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR, WorkflowFamily.STEWARD,
        ):
            raise ValueError("continuation capture requires a fixed secondary binding")
        if self.fix_infra_ceiling is not None and self.binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY:
            raise ValueError("fix infrastructure policy belongs only to issue recovery")
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
    BUDGET = "budget"
    INFRA = "infra"
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
    machine_block: RecordedMachineBlock | None = field(default=None, repr=False)

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
        if self.machine_block is not None and (not isinstance(self.machine_block, RecordedMachineBlock)
                or self.machine_block.source.attempt.run_id != self.admission.attempt.run_id
                or self.machine_block.source.attempt.leg_id != self.admission.attempt.leg_id
                or self.machine_block.pr != self.admission.attempt.binding.fence.key.target.number):
            raise ValueError("machine cause must come from the same paid issue/PR run")


@dataclass(frozen=True)
class StewardPurchase:
    capture: ContinuationCapture
    lane: StewardLane
    baseline_at: str
    stall_ceiling: int
    content_signature: str = ""
    infra_ceiling: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.capture, ContinuationCapture) or self.capture.binding.authority.family is not WorkflowFamily.STEWARD:
            raise ValueError("steward purchase requires its family capture")
        if not isinstance(self.lane, StewardLane):
            raise ValueError("steward purchase requires a fixed lane")
        _timestamp(self.baseline_at, "selected steward baseline")
        validate_workflow_integer(self.stall_ceiling, "steward stall ceiling", minimum=1)
        if self.infra_ceiling is not None:
            validate_workflow_integer(self.infra_ceiling, "steward infrastructure ceiling")
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
    infra_ceiling: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectedSteward):
            raise ValueError("steward input requires current selected authority")
        StewardPurchase(ContinuationCapture(self.selection.binding, self.selection.revision, "input"),
                        self.lane, self.input_at, self.stall_ceiling, self.content_signature, self.infra_ceiling)
        if not self.input_at:
            raise ValueError("steward input requires a nonempty baseline")


class StewardFailureKind(str, Enum):
    PAID = "paid_attempt"
    INFRASTRUCTURE = "infrastructure"
    RECLAIMED = "reclaimed_death"
    DETERMINISTIC = "deterministic_death"
    NOT_LAUNCHED = "not_launched"


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


def steward_infra_charge(outcome: StewardReturned, ceiling: int | None) -> int:
    if ceiling is not None:
        validate_workflow_integer(ceiling, "captured steward infrastructure ceiling")
    if isinstance(outcome, StewardFailed):
        if outcome.kind is StewardFailureKind.DETERMINISTIC:
            return max(1, ceiling or 0)
        if outcome.kind in {StewardFailureKind.INFRASTRUCTURE, StewardFailureKind.RECLAIMED}:
            return 1
    return 0


def steward_return_state(selection: SelectedSteward, outcome: StewardReturned, *,
                         previous: StewardFailed | None = None, infra_ceiling: int | None = None) -> StewardState | None:
    if not isinstance(outcome, (StewardFailed, StewardVerdict, StewardStopped, GroomNoFinding)):
        raise ValueError("steward return requires a classified immutable outcome")
    source = outcome.source
    if previous is not None:
        if (previous.source != source or previous.kind not in {
                StewardFailureKind.RECLAIMED, StewardFailureKind.DETERMINISTIC}):
            raise ValueError("steward refinement requires its exact recovered admission")
        marks = selection.component.watermarks
        if (source.admission.attempt.binding != selection.binding
                or marks.last_admitted != source.admission.admission_version
                or marks.last_applied != source.admission.admission_version
                or marks.last_admitted_fence != source.admission.attempt.binding.fence):
            return None
    elif not continuation_return_is_current(selection, source.admission):
        return None
    lane = selection.component.state.triage if source.lane is StewardLane.TRIAGE else selection.component.state.groom
    if (lane.attempted_at != source.input_at or lane.pending_delivery
            or source.lane is StewardLane.GROOM and selection.component.state.groom_content.attempted != source.content_signature):
        raise ValueError("steward return disagrees with its selected input episode")
    if previous is not None:
        charge = steward_infra_charge(previous, infra_ceiling)
        if lane.infra_failures < charge:
            raise ValueError("steward recovered contribution exceeds its selected episode")
        lane = replace(lane, infra_failures=lane.infra_failures - charge)
    if isinstance(outcome, GroomNoFinding):
        lane = replace(lane, baseline_at=source.input_at, stalls=0)
    elif isinstance(outcome, (StewardVerdict, StewardStopped)):
        lane = replace(lane, pending_delivery=outcome.commands, delivery_source=source,
                       stalls=lane.stalls + isinstance(outcome, StewardStopped))
    else:
        lane = replace(lane, stalls=lane.stalls + (outcome.kind is StewardFailureKind.PAID),
                       infra_failures=lane.infra_failures + steward_infra_charge(outcome, infra_ceiling))
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


def triage_admissible(selection: StewardSelection, input_at: str, ceiling: int, *, infra_ceiling: int = 0) -> bool:
    if not isinstance(selection, SelectedSteward):
        return False
    try:
        consume_steward(selection, StewardPurchase(
            ContinuationCapture(selection.binding, selection.revision, "triage:preflight"),
            StewardLane.TRIAGE, input_at, ceiling, infra_ceiling=infra_ceiling), selection.revision.version + 1)
        return True
    except ValueError:
        return False


def groom_admissible(selection: StewardSelection, input_at: str, signature: str, ceiling: int, *, infra_ceiling: int = 0) -> bool:
    if not isinstance(selection, SelectedSteward):
        return False
    try:
        consume_steward(selection, StewardPurchase(
            ContinuationCapture(selection.binding, selection.revision, "groom:preflight"),
            StewardLane.GROOM, input_at, ceiling, signature, infra_ceiling), selection.revision.version + 1)
        return True
    except ValueError:
        return False


ContinuationPurchase = IssueInfraPurchase | IssueRecoveryPurchase | IssueReplyPurchase | IssueCIPurchase | IssueReviewPurchase | IssueRebasePurchase | PRRepairPurchase | StewardPurchase


def apply_fix_infra(selection: SelectedIssueRecovery, admission: ContinuationAdmission,
                   outcome: FixInfraOutcome, previous: FixInfraReceipt | None = None,
                   *, failure_charge: int = 1) -> tuple[ActiveIssueRecovery | None, FixInfraReceipt]:
    if (not isinstance(selection, SelectedIssueRecovery) or not isinstance(admission, ContinuationAdmission)
            or not isinstance(outcome, FixInfraOutcome)):
        raise ValueError("fix infrastructure application requires selected typed evidence")
    validate_workflow_integer(failure_charge, "fix infrastructure failure charge", minimum=1)
    binding = admission.attempt.binding
    if binding.fence.key != selection.binding.fence.key or binding.authority.family is not WorkflowFamily.ISSUE_RECOVERY:
        raise ValueError("fix infrastructure evidence belongs to another issue")
    normal = outcome is FixInfraOutcome.RETURNED_NORMAL
    charge = 0 if normal else failure_charge
    if previous is not None:
        if not isinstance(previous, FixInfraReceipt) or previous.admission != admission:
            raise ValueError("fix infrastructure retry differs from its admitted run")
        if previous.outcome in (FixInfraOutcome.RETURNED_INFRA, FixInfraOutcome.RETURNED_NORMAL):
            if previous.outcome is outcome or outcome in (FixInfraOutcome.RECLAIMED, FixInfraOutcome.DETERMINISTIC_DEATH):
                return None, previous
            raise ValueError("fix infrastructure returned classification conflicts")
        if outcome is FixInfraOutcome.RECLAIMED or previous.outcome is outcome:
            return None, previous
        if outcome is FixInfraOutcome.RETURNED_INFRA:
            return None, replace(previous, outcome=outcome)
        if outcome is FixInfraOutcome.DETERMINISTIC_DEATH:
            charge = max(0, charge - previous.charge)
    state = selection.component.state
    refused = replace(previous, outcome=outcome, charge=0 if normal else previous.charge) if previous is not None else FixInfraReceipt(
        admission, outcome, state.infra_failures, None, 0)
    if (binding.fence.generation > selection.revision.fence.generation
            or binding == selection.binding and admission.admission_version > selection.revision.version):
        raise ValueError("fix infrastructure evidence has a future admission fence")
    if binding != selection.binding:
        return None, refused
    episode = state.infra_episode
    same_generation = episode is not None and episode.source.attempt.binding.fence == binding.fence
    latest = episode.latest_version if same_generation and episode is not None else 0
    reset = episode.reset_version if same_generation and episode is not None else 0
    version = admission.admission_version
    if (normal and version < latest or not normal and version <= reset):
        return None, refused
    failures = 0 if normal else state.infra_failures + charge
    episode = FixInfraEpisode(admission, max(latest, version), version if normal else reset)
    after = replace(selection.component, state=replace(state, infra_failures=failures, infra_episode=episode))
    contribution = 0 if normal else charge + (previous.charge if previous is not None else 0)
    return after, FixInfraReceipt(admission, outcome, failures, episode, contribution)


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
            or (purchase.infra_ceiling is not None and purchase.infra_ceiling > 0
                and lane.infra_failures >= purchase.infra_ceiling and not newer_input)
            or purchase.lane is StewardLane.TRIAGE and not reply_advances(purchase.baseline_at, lane.baseline_at)):
        raise ValueError("steward purchase is held, pending delivery or exhausted")
    lane = replace(lane, attempted_at=purchase.baseline_at, stalls=0 if newer_input else lane.stalls,
                   infra_failures=0 if newer_input else lane.infra_failures)
    state = replace(component.state, **{purchase.lane.value: lane})
    if purchase.lane is StewardLane.GROOM:
        state = replace(state, groom_content=replace(content, attempted=purchase.content_signature))
    component = replace(component, state=state)
    return replace(component, watermarks=component.watermarks.admit(version, selection.revision.fence))
