"""Bounded split-leg context and exact contract provenance, not a task queue."""

from dataclasses import dataclass
from datetime import datetime
import hashlib

from .contracts import WorkflowAttemptIdentity
from .effort import Effort
from .outcome import Adjudication
from .refs import issue_from_branch
from .workflow_identity import (
    IssueRef, WorkflowBinding, validate_workflow_identifier,
    validate_workflow_integer, validate_workflow_text,
)

MAX_SPLIT_TRAIL = 4


def validate_contract_rounds(value: int) -> None:
    validate_workflow_integer(value, "adjudication ceiling")
    if value > MAX_SPLIT_TRAIL:
        raise ValueError(f"adjudication ceiling cannot exceed {MAX_SPLIT_TRAIL} retained rounds")


@dataclass(frozen=True)
class ContractRevision:
    branch: str
    head_sha: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.branch, "contract branch")
        if issue_from_branch(self.branch) is None:
            raise ValueError("contract revision requires an issue-owned branch")
        if not isinstance(self.head_sha, str) or len(self.head_sha) not in (40, 64) or any(char not in "0123456789abcdef" for char in self.head_sha):
            raise ValueError("contract revision requires a complete Git object ID")


@dataclass(frozen=True)
class ContractBaseline:
    source: WorkflowAttemptIdentity
    revision: ContractRevision

    def __post_init__(self) -> None:
        if not isinstance(self.source, WorkflowAttemptIdentity) or not isinstance(self.revision, ContractRevision):
            raise ValueError("contract baseline requires harness-observed revision and admitted source")
        if not isinstance(self.source.binding.fence.key.target, IssueRef) or issue_from_branch(self.revision.branch) != self.source.binding.fence.key.target.number:
            raise ValueError("contract baseline belongs to another issue")


@dataclass(frozen=True)
class UnavailableContractRevision:
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "contract revision error", limit=4000)


@dataclass(frozen=True)
class SplitSelection:
    runtime: str
    model: str
    effort: Effort | None = None
    difficulty_notice: str = ""

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.runtime, "split runtime")
        validate_workflow_identifier(self.model, "split selected model")
        if self.effort is not None and not isinstance(self.effort, Effort):
            raise ValueError("split selection requires typed effort")
        validate_workflow_text(self.difficulty_notice, "difficulty notice", limit=4000, empty=True)


@dataclass(frozen=True)
class SplitReply:
    author: str
    created_at: str
    body: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.author, "reply author")
        validate_workflow_identifier(self.created_at, "reply identity")
        validate_workflow_text(self.body, "reply body", limit=65536)


@dataclass(frozen=True)
class AdjudicationStep:
    attempt: WorkflowAttemptIdentity
    admission_version: int
    verdict: Adjudication
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, WorkflowAttemptIdentity) or not isinstance(self.verdict, Adjudication):
            raise ValueError("adjudication trail requires admitted source and typed verdict")
        validate_workflow_integer(self.admission_version, "adjudication admission", minimum=1)
        validate_workflow_text(self.rationale, "adjudication rationale", limit=4000)


def _same_episode(source: WorkflowAttemptIdentity, binding: WorkflowBinding) -> bool:
    prior = source.binding
    return (prior.fence.key, prior.authority, prior.episode_id) == (binding.fence.key, binding.authority, binding.episode_id) and prior.fence.generation <= binding.fence.generation


@dataclass(frozen=True)
class SplitContext:
    branch: str
    selection: SplitSelection
    reply: SplitReply | None = None
    trail: tuple[AdjudicationStep, ...] = ()

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.branch, "split branch")
        if issue_from_branch(self.branch) is None:
            raise ValueError("split context requires an issue-owned branch")
        if not isinstance(self.selection, SplitSelection) or (self.reply is not None and not isinstance(self.reply, SplitReply)):
            raise ValueError("split context requires validated selection and observed reply")
        if not isinstance(self.trail, tuple) or len(self.trail) > MAX_SPLIT_TRAIL or any(not isinstance(item, AdjudicationStep) for item in self.trail):
            raise ValueError("split trail must be a bounded tuple of adjudications")
        if len({item.attempt.run_id for item in self.trail}) != len(self.trail):
            raise ValueError("adjudication trail cannot repeat an admitted run")
        if any(before.admission_version >= after.admission_version or not _same_episode(before.attempt, after.attempt.binding)
               for before, after in zip(self.trail, self.trail[1:])):
            raise ValueError("adjudication trail must follow admission order within one episode")

    @property
    def human_reply(self) -> str:
        return self.reply.body if self.reply is not None else ""

    @property
    def uphold_note(self) -> str:
        return self.trail[-1].rationale if self.trail and self.trail[-1].verdict is Adjudication.UPHELD else ""


def _cursor(source: WorkflowAttemptIdentity, version: int, context: SplitContext, baseline: ContractBaseline) -> None:
    if not isinstance(source, WorkflowAttemptIdentity) or not isinstance(context, SplitContext) or not isinstance(baseline, ContractBaseline):
        raise ValueError("split cursor requires source, context and contract baseline")
    validate_workflow_integer(version, "split cursor admission", minimum=1)
    if baseline.revision.branch != context.branch or not _same_episode(baseline.source, source.binding) or any(not _same_episode(item.attempt, source.binding) or item.admission_version > version for item in context.trail):
        raise ValueError("split cursor cannot inherit foreign or future provenance")


@dataclass(frozen=True)
class SplitImplement:
    source: WorkflowAttemptIdentity
    admission_version: int
    context: SplitContext
    baseline: ContractBaseline

    def __post_init__(self) -> None:
        _cursor(self.source, self.admission_version, self.context, self.baseline)


@dataclass(frozen=True)
class NeedsAdjudication:
    """Ready dispute; source is its latest paid predecessor, not its origin.

    An infrastructure-failed adjudication advances this predecessor so its
    successor needs a new reset. Immutable returned inputs retain the original
    implementation objection and the intervening paid provenance chain.
    """

    source: WorkflowAttemptIdentity
    admission_version: int
    context: SplitContext
    baseline: ContractBaseline
    objection: str

    def __post_init__(self) -> None:
        _cursor(self.source, self.admission_version, self.context, self.baseline)
        validate_workflow_text(self.objection, "contract objection", limit=4000)


SplitContinuation = SplitImplement | NeedsAdjudication


@dataclass(frozen=True)
class ContractResetIntent:
    binding: WorkflowBinding
    petition: NeedsAdjudication

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WorkflowBinding) or not isinstance(self.petition, NeedsAdjudication) or not _same_episode(self.petition.source, self.binding):
            raise ValueError("contract reset requires the current episode's admitted dispute")

    @property
    def intent_id(self) -> str:
        parts = (self.binding.fence.key.repository, str(self.binding.fence.key.target.number), str(self.binding.fence.generation),
                 str(self.binding.authority.epoch), self.binding.episode_id, str(self.petition.source.run_id), self.petition.source.leg_id,
                 self.petition.baseline.revision.branch, self.petition.baseline.revision.head_sha)
        return "contract-reset:" + hashlib.sha256("\0".join(parts).encode()).hexdigest()

    @property
    def archive_ref(self) -> str:
        return "refs/issue-agent/contract-reset/" + hashlib.sha256(self.intent_id.encode()).hexdigest()

    @property
    def source(self) -> WorkflowAttemptIdentity:
        return self.petition.source

    @property
    def baseline(self) -> ContractBaseline:
        return self.petition.baseline


@dataclass(frozen=True)
class ReauthorResetIntent:
    """Replacement contract writing must not observe discarded implementation."""

    binding: WorkflowBinding
    source: WorkflowAttemptIdentity
    baseline: ContractBaseline
    input_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WorkflowBinding) or not isinstance(self.source, WorkflowAttemptIdentity) or not isinstance(self.baseline, ContractBaseline):
            raise ValueError("re-author reset requires admitted source and contract baseline")
        if not _same_episode(self.source, self.binding) or not _same_episode(self.baseline.source, self.binding):
            raise ValueError("re-author reset cannot inherit foreign contract provenance")
        fingerprint = self.input_fingerprint
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            raise ValueError("re-author reset requires the replacement input SHA-256 digest")

    @property
    def intent_id(self) -> str:
        parts = (self.binding.fence.key.repository, str(self.binding.fence.key.target.number), str(self.binding.fence.generation),
                 str(self.binding.authority.epoch), self.binding.episode_id, str(self.source.run_id), self.source.leg_id,
                 self.baseline.revision.branch, self.baseline.revision.head_sha, self.input_fingerprint)
        return "reauthor-reset:" + hashlib.sha256("\0".join(parts).encode()).hexdigest()

    @property
    def archive_ref(self) -> str:
        return "refs/issue-agent/contract-reset/" + hashlib.sha256(self.intent_id.encode()).hexdigest()


ResetIntent = ContractResetIntent | ReauthorResetIntent


@dataclass(frozen=True)
class ContractResetResult:
    revision: ContractRevision
    archive_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.revision, ContractRevision):
            raise ValueError("contract reset requires a verified revision")
        validate_workflow_identifier(self.archive_ref, "retained work reference")
        prefix = "refs/issue-agent/contract-reset/"
        suffix = self.archive_ref.removeprefix(prefix)
        if not self.archive_ref.startswith(prefix) or len(suffix) != 64 or any(char not in "0123456789abcdef" for char in suffix):
            raise ValueError("reset archive must use the harness-owned reference namespace")


@dataclass(frozen=True)
class ContractResetCompleted:
    intent: ResetIntent
    result: ContractResetResult
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.intent, (ContractResetIntent, ReauthorResetIntent)) or not isinstance(self.result, ContractResetResult) or self.result.revision != self.intent.baseline.revision:
            raise ValueError("contract reset acknowledgement disagrees with its exact intent")
        if self.result.archive_ref != self.intent.archive_ref:
            raise ValueError("contract reset archive belongs to another intent")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("contract reset acknowledgement requires an aware observation time")


@dataclass(frozen=True)
class AdjudicationInput:
    reset: ContractResetCompleted

    def __post_init__(self) -> None:
        if not isinstance(self.reset, ContractResetCompleted) or not isinstance(self.reset.intent, ContractResetIntent):
            raise ValueError("adjudication requires a verified reset acknowledgement")

    @property
    def petition(self) -> NeedsAdjudication:
        assert isinstance(self.reset.intent, ContractResetIntent)
        return self.reset.intent.petition


@dataclass(frozen=True)
class ReauthorInput:
    reset: ContractResetCompleted
    context: SplitContext

    def __post_init__(self) -> None:
        if not isinstance(self.reset, ContractResetCompleted) or not isinstance(self.reset.intent, ReauthorResetIntent):
            raise ValueError("replacement author requires a verified re-author reset")
        if not isinstance(self.context, SplitContext) or self.context.trail or self.context.branch != self.reset.intent.baseline.revision.branch:
            raise ValueError("replacement author requires fresh context on the reset branch")


SplitInput = SplitContext | SplitImplement | AdjudicationInput | ReauthorInput


def split_context(value: SplitInput) -> SplitContext:
    if isinstance(value, SplitContext):
        return value
    if isinstance(value, (SplitImplement, ReauthorInput)):
        return value.context
    if isinstance(value, AdjudicationInput):
        return value.petition.context
    raise ValueError("split input must be a validated stage input")


def split_sources(value: SplitContinuation) -> tuple[WorkflowAttemptIdentity, ...]:
    return (value.source, value.baseline.source) + tuple(item.attempt for item in value.context.trail)
