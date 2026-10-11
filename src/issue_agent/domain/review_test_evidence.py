"""Independent execution evidence; model verdicts cannot mint these facts."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import re

from .contracts import TestExecution, TestExecutionStatus
from .review_execution import ReviewExecutionBinding, ReviewRoundIdentity
from .review_rejection import validate_commit_sha
from .workflow_identity import WorkflowBinding, validate_workflow_identifier

# Fits a failing cargo/vitest summary after its echoed command line (#274 follow-up),
# far below GitHub's 65,536-character comment limit.
SUMMARY_LIMIT = 16384


EXECUTOR_POLICY = "ordinary-test-v1"
ENVIRONMENT_POLICY = "target-child-v1"


def failed_test_review_body(summary: str, exit_code: int | None) -> str:
    """Deterministic markdown within the submission bound, including marker room."""
    header = "Independent operator tests failed on this head"
    header += f" (exit {exit_code})." if exit_code is not None else "."
    output = summary.removeprefix("failed\n")[-SUMMARY_LIMIT:]

    def render(tail: str) -> str:
        fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", tail)), default=0))
        preview = "\n".join([line for line in tail.splitlines() if line.strip()][-25:])
        return (f"{header}\n\n{fence}\n{preview}\n{fence}\n\n"
                "<details><summary>Full test output (tail)</summary>\n\n"
                f"{fence}\n{tail}\n{fence}\n\n</details>")

    body = render(output)
    # Four fences plus two copies of the tail cost at most six times its length.
    limit = 65536 - 1024 - 256  # marker and optional rejection-ceiling preamble
    return body if len(body) <= limit else render(output[-(limit - len(header) - 256) // 6:])


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _digest(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("test evidence requires a SHA-256 digest")


@dataclass(frozen=True)
class OperatorTestCommand:
    command: str

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command.strip() or len(self.command) > 65536 or "\x00" in self.command:
            raise ValueError("operator test command must be bounded nonempty text")


@dataclass(frozen=True)
class OperatorTestWaiver:
    provenance: str = "operator ALLOW_UNTESTED"

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.provenance, "waiver provenance")


@dataclass(frozen=True)
class MissingOperatorTestCommand:
    pass


@dataclass(frozen=True)
class ReviewTestConfiguration:
    scope: OperatorTestCommand | OperatorTestWaiver | MissingOperatorTestCommand
    failure_codes: tuple[int, ...]
    timeout_ms: int
    workload_digest: str
    config_version: str
    executor_policy: str = EXECUTOR_POLICY
    environment_policy: str = ENVIRONMENT_POLICY

    def __post_init__(self) -> None:
        if not isinstance(self.scope, (OperatorTestCommand, OperatorTestWaiver, MissingOperatorTestCommand)):
            raise ValueError("test scope must come from frozen operator configuration")
        if (not isinstance(self.failure_codes, tuple) or not self.failure_codes or len(self.failure_codes) > 32
                or len(set(self.failure_codes)) != len(self.failure_codes)
                or any(type(code) is not int or not 1 <= code <= 123 for code in self.failure_codes)):
            raise ValueError("assertion failure codes must be bounded unique positive codes")
        if type(self.timeout_ms) is not int or not 0 < self.timeout_ms <= 86400000:
            raise ValueError("test timeout must be a bounded positive millisecond duration")
        _digest(self.workload_digest)
        for name in ("config_version", "executor_policy", "environment_policy"):
            validate_workflow_identifier(getattr(self, name), name)

    @property
    def timeout_seconds(self) -> float:
        return self.timeout_ms / 1000

    @property
    def identity(self) -> str:
        return digest(repr(self))


@dataclass(frozen=True)
class ReviewTestSubject:
    rejection: WorkflowBinding
    execution: ReviewExecutionBinding
    head_sha: str
    patch_id: str
    configuration_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.rejection, WorkflowBinding) or not isinstance(self.execution, ReviewExecutionBinding) or self.execution.rejection != self.rejection:
            raise ValueError("test subject requires its exact current authority bindings")
        validate_commit_sha(self.head_sha, "test head")
        _digest(self.patch_id)
        _digest(self.configuration_digest)

    @property
    def identity(self) -> str:
        # Binding version is absent; the full admitted authority identity remains.
        return digest(repr(self))

    @property
    def gate_id(self) -> str:
        return "ordinary-test:" + self.identity


@dataclass(frozen=True)
class ReviewTestInvocation:
    subject: ReviewTestSubject
    configuration: ReviewTestConfiguration
    round: ReviewRoundIdentity
    invocation_id: str
    started_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.subject, ReviewTestSubject) or not isinstance(self.configuration, ReviewTestConfiguration) or self.subject.configuration_digest != self.configuration.identity:
            raise ValueError("test invocation requires its exact frozen configuration")
        if not isinstance(self.round, ReviewRoundIdentity) or self.round.binding != self.subject.execution:
            raise ValueError("test provenance belongs to another execution binding")
        validate_workflow_identifier(self.invocation_id, "test invocation")
        if not isinstance(self.started_at, datetime) or self.started_at.utcoffset() is None:
            raise ValueError("test invocation requires an aware start time")


class ReviewTestOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    WAIVED = "operator_waiver"


@dataclass(frozen=True)
class ReviewTestTreeProof:
    head_sha: str
    committed_tree: str
    index_tree: str
    tracked_tree: str

    def __post_init__(self) -> None:
        for name in ("head_sha", "committed_tree", "index_tree", "tracked_tree"):
            validate_commit_sha(getattr(self, name), name)
        if self.committed_tree != self.index_tree or self.index_tree != self.tracked_tree:
            raise ValueError("tracked test inputs must match the committed tree")


@dataclass(frozen=True)
class ReviewTestCompletion:
    invocation: ReviewTestInvocation
    outcome: ReviewTestOutcome
    execution: TestExecution | None
    before: ReviewTestTreeProof | None
    after: ReviewTestTreeProof | None
    summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.invocation, ReviewTestInvocation) or not isinstance(self.outcome, ReviewTestOutcome):
            raise ValueError("test completion requires typed harness evidence")
        if not isinstance(self.summary, str) or not self.summary or len(self.summary) > SUMMARY_LIMIT:
            raise ValueError("test completion summary must be bounded nonempty text")
        for proof in (self.before, self.after):
            if proof is not None and (not isinstance(proof, ReviewTestTreeProof) or proof.head_sha != self.invocation.subject.head_sha):
                raise ValueError("test tree proof belongs to another head")
        scope = self.invocation.configuration.scope
        execution = self.execution
        if execution is not None:
            if (not isinstance(execution, TestExecution) or not isinstance(scope, OperatorTestCommand)
                    or execution.command != ("bash", "-c", scope.command)
                    or execution.repository_revision != self.invocation.subject.head_sha
                    or execution.executor_identity != self.invocation.configuration.executor_policy):
                raise ValueError("execution disagrees with its frozen invocation")
            _digest(execution.stdout_digest)
            _digest(execution.stderr_digest)
        if self.outcome is ReviewTestOutcome.WAIVED:
            if not isinstance(scope, OperatorTestWaiver) or execution is not None:
                raise ValueError("waiver cannot claim test execution")
        elif self.outcome is ReviewTestOutcome.PASSED:
            if execution is None or execution.status is not TestExecutionStatus.PASSED:
                raise ValueError("pass requires actual successful operator execution")
        elif self.outcome is ReviewTestOutcome.FAILED:
            if (execution is None or execution.status is not TestExecutionStatus.TEST_FAILED
                    or execution.exit_code not in self.invocation.configuration.failure_codes):
                raise ValueError("genuine failure requires the frozen assertion code contract")
        if self.outcome is not ReviewTestOutcome.UNAVAILABLE and (self.before is None or self.before != self.after):
            raise ValueError("authorizing evidence requires equal structured pre/post tree proof")

    @property
    def approves(self) -> bool:
        return self.outcome in {ReviewTestOutcome.PASSED, ReviewTestOutcome.WAIVED}


class PostedReview(str, Enum):
    APPROVED = "APPROVE"
    CHANGES_REQUESTED = "REQUEST_CHANGES"
    VERDICT_ESCALATED = "verdict"
    CEILING_ESCALATED = "ceiling"

    @property
    def event(self) -> str:
        return "COMMENT" if self.escalated else self.value

    @property
    def escalated(self) -> bool:
        return self in {PostedReview.VERDICT_ESCALATED, PostedReview.CEILING_ESCALATED}


def posted_review(event: str, cause: str | None) -> PostedReview:
    if event == "COMMENT" and cause in {"verdict", "ceiling"}:
        return PostedReview(cause)
    if cause is None and event in {"APPROVE", "REQUEST_CHANGES"}:
        return PostedReview(event)
    raise ValueError("contradictory posted review outcome")


def test_only_rejection_release_eligible(
    *, event: str, reference: object, completion: ReviewTestCompletion | None,
    rejection: WorkflowBinding, execution: ReviewExecutionBinding, head: str, patch: str,
) -> bool:
    """Only approval conversion carries a test ref; findings never acquire one.

    The caller authenticates the registered submission and loads completion
    from harness-owned storage. Neither review prose nor fixer claims is evidence.
    """
    if (event != "REQUEST_CHANGES" or not isinstance(reference, str)
            or not isinstance(completion, ReviewTestCompletion)
            or completion.outcome is not ReviewTestOutcome.FAILED):
        return False
    invocation = completion.invocation
    subject = invocation.subject
    return (reference == invocation.invocation_id and subject.rejection == rejection
            and subject.execution == execution and subject.head_sha == head and subject.patch_id == patch)


def ordinary_test_required_fact(subject: ReviewTestSubject) -> str:
    return f"ordinary test {subject.head_sha} {digest(repr(subject.execution))} {subject.identity}"


def ordinary_test_hold(gate: object, execution: ReviewExecutionBinding, head: str | None = None) -> bool:
    from .workflow import EvidenceUnavailable, OpenGate
    if head is None and isinstance(gate, OpenGate) and isinstance(gate.cause, EvidenceUnavailable):
        fields = gate.cause.required_fact.split()
        head = fields[2] if len(fields) == 5 else None
    return (isinstance(gate, OpenGate) and isinstance(gate.cause, EvidenceUnavailable)
        and gate.gate_id.startswith("ordinary-test:")
        and gate.cause.required_fact == f"ordinary test {head} {digest(repr(execution))} {gate.gate_id.removeprefix('ordinary-test:')}")
