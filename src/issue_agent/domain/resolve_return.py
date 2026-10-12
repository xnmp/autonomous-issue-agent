"""Unclassified paid returns: evidence, not a second workflow allowance."""

from dataclasses import dataclass
from datetime import datetime

from .contracts import CommandId, WorkflowAttemptIdentity
from .outcome import AgentResult, ExitReason
from .workflow import (
    LinkedPrPresent, NoLinkedPr, ObservedIssue, ResolveAttemptResult,
    ResolveSession, ResolveStage, _command_ids, _count, _text, _time,
)
from .workflow_identity import WorkflowBinding


@dataclass(frozen=True)
class ResolveInputs:
    title: str
    body: str
    author: str

    def __post_init__(self) -> None:
        for name in ("title", "body", "author"):
            _text(getattr(self, name), name, limit=65536, empty=True)


@dataclass(frozen=True)
class ReturnedResolve:
    attempt: WorkflowAttemptIdentity
    stage: ResolveStage
    inputs: ResolveInputs
    is_error: bool
    status: str
    summary: str
    needs: str
    envelope_subtype: str
    exit_reason: ExitReason | None
    session: ResolveSession | None
    returned_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, WorkflowAttemptIdentity):
            raise ValueError("returned resolve requires its accounting-owned attempt")
        if not isinstance(self.stage, ResolveStage) or self.stage is ResolveStage.ROUTE:
            raise ValueError("returned resolve requires a supported work stage")
        if not isinstance(self.inputs, ResolveInputs) or type(self.is_error) is not bool:
            raise ValueError("returned resolve requires validated inputs and outcome")
        for name in ("status", "envelope_subtype"):
            _text(getattr(self, name), name, limit=512, empty=True)
        for name in ("summary", "needs"):
            _text(getattr(self, name), name, limit=65536, empty=True)
        if self.exit_reason is not None and not isinstance(self.exit_reason, ExitReason):
            raise ValueError("returned resolve requires a typed exit reason")
        if self.session is not None and (
            not isinstance(self.session, ResolveSession) or self.session.source != self.attempt
        ):
            raise ValueError("returned session must belong to this attempt")
        _time(self.returned_at, "returned time")

    def result(self) -> AgentResult:
        return AgentResult(
            is_error=self.is_error, status=self.status, summary=self.summary,
            needs=self.needs, envelope_subtype=self.envelope_subtype,
            exit_reason=self.exit_reason,
            session_id=self.session.session_id if self.session is not None else "",
            workflow_attempt=self.attempt,
        )


@dataclass(frozen=True)
class PendingResolveReturn:
    returned: ReturnedResolve
    next_read_at: datetime
    failed_reads: int = 0
    last_error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.returned, ReturnedResolve):
            raise ValueError("pending observation requires a returned resolve receipt")
        _time(self.next_read_at, "next read time")
        if self.next_read_at < self.returned.returned_at:
            raise ValueError("observation cannot precede the returned result")
        _count(self.failed_reads, "failed observation reads")
        _text(self.last_error, "last observation error", limit=16384, empty=True)


@dataclass(frozen=True)
class UnavailableResolveReturn:
    """A read failure is not absence and must not hide healthy recovery peers."""

    record_key: str
    reason: str
    next_read_at: datetime

    def __post_init__(self) -> None:
        _text(self.record_key, "diagnostic record key", limit=512)
        _text(self.reason, "unavailable reason", limit=16384)
        _time(self.next_read_at, "next read time")


@dataclass(frozen=True)
class FinalizedResolveReturn:
    result: ResolveAttemptResult
    issue: ObservedIssue
    linkage: NoLinkedPr | LinkedPrPresent
    commands: tuple[CommandId, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.result, ResolveAttemptResult) or not isinstance(self.issue, ObservedIssue) or not isinstance(self.linkage, (NoLinkedPr, LinkedPrPresent)):
            raise ValueError("finalized return requires its classification and observations")
        key = self.result.attempt.binding.fence.key
        if self.issue.key != key or self.linkage.issue != key:
            raise ValueError("finalized return observations belong to another workflow")
        _command_ids(self.commands, "completion commands")


@dataclass(frozen=True)
class RetiredResolveReturn:
    attempt: WorkflowAttemptIdentity
    superseded_by: WorkflowBinding

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, WorkflowAttemptIdentity) or not isinstance(self.superseded_by, WorkflowBinding):
            raise ValueError("retired return requires captured and current authority")
        before = self.attempt.binding
        if before == self.superseded_by or before.fence.key != self.superseded_by.fence.key:
            raise ValueError("retirement requires changed authority for the same workflow")


@dataclass(frozen=True)
class ResolveCleanupCompleted:
    """App-owned acknowledgement of idempotent local cleanup under effect authority."""

    attempt: WorkflowAttemptIdentity
    issue: ObservedIssue
    linkage: NoLinkedPr

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, WorkflowAttemptIdentity) or not isinstance(self.issue, ObservedIssue) or not isinstance(self.linkage, NoLinkedPr):
            raise ValueError("cleanup requires admitted identity and terminal observations")
        if self.issue.key != self.attempt.binding.fence.key or self.linkage.issue != self.issue.key or self.issue.issue.state != "closed":
            raise ValueError("cleanup must concern this closed issue without a linked PR")
