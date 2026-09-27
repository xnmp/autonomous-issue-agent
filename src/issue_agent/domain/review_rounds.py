"""Outer-round finalization from harness-verified plans and terminal evidence.

These values name durable evidence; constructors cannot verify its existence.
Adapters must join receipts to the exact round/claim occurrence and fence late
admission/submission before minting abandonment or no-launch facts.
"""

from dataclasses import dataclass
from enum import Enum

from .agent_task import RUNTIME_CAPABILITIES, RuntimeName
from .outcome import ExitReason
from .review import ReviewMode
from .review_execution import (
    MAX_ROUND_CHILDREN, AddReviewStrike, ClearReviewSession, CopiedReviewProgress,
    LaunchedReviewChild, NoReviewSession, PaidReviewProgress, PreserveReviewSession,
    ReplaceReviewSession, ReusableReviewSession, ReviewAdmissionRefused,
    ReviewCancellationReason, ReviewChildIdentity, ReviewChildKind,
    ReviewInfrastructureDeath, ReviewPinDeferred, ReviewPreparationDeferred, ReviewPreparationFailure,
    ReviewRoundCancelled, ReviewRoundFailure, ReviewRoundIdentity, ReviewRoundOutcome,
    ReviewUnlaunchedRound, SaturateReviewFailures, SetDisplayedRound,
)
from .review_rejection import validate_commit_sha
from .review_return import ReviewReturnedEnvelope
from .workflow_identity import (
    validate_workflow_identifier as identifier,
    validate_workflow_integer as integer,
)

MAX_REVIEW_STAGES = 32
MAX_ROUND_SUBMISSIONS = 16


def _tuple(values: object, variants: tuple[type, ...], limit: int, name: str) -> None:
    if not isinstance(values, tuple) or len(values) > limit or any(not isinstance(value, variants) for value in values):
        raise ValueError(f"{name} must be a bounded tuple of typed evidence")


@dataclass(frozen=True)
class ReviewStagePlan:
    stage_id: str
    kind: ReviewChildKind
    children: tuple[ReviewChildIdentity, ...]

    def __post_init__(self) -> None:
        identifier(self.stage_id, "planned review stage")
        if not isinstance(self.kind, ReviewChildKind):
            raise ValueError("review stage requires a typed child role")
        _tuple(self.children, (ReviewChildIdentity,), MAX_ROUND_CHILDREN, "planned children")
        if not self.children or any(child.stage_id != self.stage_id or child.kind is not self.kind for child in self.children):
            raise ValueError("a stage must contain its own planned children")
        if len({child.child_id for child in self.children}) != len(self.children):
            raise ValueError("planned children must have distinct identities")
        if len({child.round for child in self.children}) != 1:
            raise ValueError("a planned stage belongs to one outer round")
        if self.kind in (ReviewChildKind.GATE, ReviewChildKind.REVIEWER) and len(self.children) != 1:
            raise ValueError("a gate or normal review stage has exactly one invocation")


class ReviewSubmissionKind(str, Enum):
    PAID = "paid"
    COPIED = "copied"


@dataclass(frozen=True)
class ReviewSubmissionPlan:
    command_id: str
    kind: ReviewSubmissionKind

    def __post_init__(self) -> None:
        identifier(self.command_id, "planned review submission")
        if not isinstance(self.kind, ReviewSubmissionKind):
            raise ValueError("submission plan requires its paid/copy provenance")


class ReviewSessionReuse(str, Enum):
    REUSABLE = "reusable"
    FRESH = "fresh"


@dataclass(frozen=True)
class ReviewRoundPlan:
    round: ReviewRoundIdentity
    head_sha: str
    mode: ReviewMode
    runtime: RuntimeName
    session_reuse: ReviewSessionReuse
    displayed_round: int
    failure_ceiling: int
    stages: tuple[ReviewStagePlan, ...] = ()
    submissions: tuple[ReviewSubmissionPlan, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.round, ReviewRoundIdentity) or not isinstance(self.mode, ReviewMode):
            raise ValueError("round plan requires an identity and selected mode")
        validate_commit_sha(self.head_sha, "planned review head")
        if not isinstance(self.runtime, RuntimeName) or not isinstance(self.session_reuse, ReviewSessionReuse):
            raise ValueError("round plan requires captured runtime/session policy")
        integer(self.displayed_round, "planned displayed round", minimum=1)
        integer(self.failure_ceiling, "captured failure ceiling")
        _tuple(self.stages, (ReviewStagePlan,), MAX_REVIEW_STAGES, "review stages")
        _tuple(self.submissions, (ReviewSubmissionPlan,), MAX_ROUND_SUBMISSIONS, "planned submissions")
        if len({stage.stage_id for stage in self.stages}) != len(self.stages):
            raise ValueError("round stage identities must be unique")
        children = self.children
        if len(children) > MAX_ROUND_CHILDREN or len({child.child_id for child in children}) != len(children):
            raise ValueError("outer round requires bounded distinct children")
        if any(child.round != self.round for child in children):
            raise ValueError("planned child belongs to another round")
        if len({submission.command_id for submission in self.submissions}) != len(self.submissions):
            raise ValueError("one submission command cannot appear twice in a round plan")
        kinds = tuple(stage.kind for stage in self.stages)
        if self.mode is ReviewMode.MULTI:
            if kinds and (kinds[0] is not ReviewChildKind.GATE or kinds.count(ReviewChildKind.GATE) != 1
                          or ReviewChildKind.REVIEWER in kinds or kinds.count(ReviewChildKind.DIMENSION) > 1):
                raise ValueError("multi review requires its gate before specialist stages")
            if ReviewChildKind.VALIDATOR in kinds and (
                ReviewChildKind.DIMENSION not in kinds
                or kinds.index(ReviewChildKind.VALIDATOR) < kinds.index(ReviewChildKind.DIMENSION)
            ):
                raise ValueError("validators require the preceding dimension stage")
        elif kinds not in ((), (ReviewChildKind.REVIEWER,)):
            raise ValueError("single/delegated review has one normal reviewer stage")

    @property
    def children(self) -> tuple[ReviewChildIdentity, ...]:
        return tuple(child for stage in self.stages for child in stage.children)


@dataclass(frozen=True)
class ReviewChildPending:
    child: ReviewChildIdentity

    def __post_init__(self) -> None:
        _child(self.child)


def _child(value: ReviewChildIdentity) -> None:
    if not isinstance(value, ReviewChildIdentity):
        raise ValueError("child evidence requires its planned identity")


@dataclass(frozen=True)
class ReviewChildRefused:
    child: ReviewChildIdentity
    refusal_receipt: str

    def __post_init__(self) -> None:
        _child(self.child)
        identifier(self.refusal_receipt, "child admission refusal")


@dataclass(frozen=True)
class ReviewChildReleased:
    child: ReviewChildIdentity
    run_id: str
    release_receipt: str

    def __post_init__(self) -> None:
        _child(self.child)
        identifier(self.run_id, "unlaunched child run")
        identifier(self.release_receipt, "verified unlaunched release")


@dataclass(frozen=True)
class ReviewChildSettledWithoutLaunch:
    child: ReviewChildIdentity
    run_id: str
    settlement_receipt: str
    no_launch_receipt: str
    preparation: ReviewPreparationFailure

    def __post_init__(self) -> None:
        _child(self.child)
        identifier(self.run_id, "unlaunched settled run")
        identifier(self.settlement_receipt, "unlaunched settlement receipt")
        identifier(self.no_launch_receipt, "verified no-launch evidence")
        if not isinstance(self.preparation, ReviewPreparationFailure):
            raise ValueError("unlaunched runtime settlement requires its preparation failure")


@dataclass(frozen=True)
class ReviewChildNotAdmitted:
    """Verified absence after the exact parent claim occurrence was fenced."""

    child: ReviewChildIdentity
    abandonment_receipt: str

    def __post_init__(self) -> None:
        _child(self.child)
        identifier(self.abandonment_receipt, "unadmitted child abandonment evidence")


@dataclass(frozen=True)
class ReviewChildPinDeferred:
    child: ReviewChildIdentity
    preflight_receipt: str

    def __post_init__(self) -> None:
        _child(self.child)
        identifier(self.preflight_receipt, "child pin deferral")


@dataclass(frozen=True)
class ReviewChildSucceeded:
    session: NoReviewSession | ReusableReviewSession = NoReviewSession()

    def __post_init__(self) -> None:
        if not isinstance(self.session, (NoReviewSession, ReusableReviewSession)):
            raise ValueError("successful child requires a typed session result")


@dataclass(frozen=True)
class OrdinaryReviewFailure:
    pass


@dataclass(frozen=True)
class DeterministicReviewFailure:
    classification_receipt: str

    def __post_init__(self) -> None:
        identifier(self.classification_receipt, "deterministic child failure evidence")


@dataclass(frozen=True)
class ReviewChildFailed:
    reason: ExitReason
    determinism: OrdinaryReviewFailure | DeterministicReviewFailure = OrdinaryReviewFailure()

    def __post_init__(self) -> None:
        if not isinstance(self.reason, ExitReason) or self.reason in (
            ExitReason.OK, ExitReason.OPERATOR_CANCELLED, ExitReason.KILLED_EXTERNALLY,
            ExitReason.RUNTIME_UNAVAILABLE, ExitReason.BUDGET_EXHAUSTED,
        ):
            raise ValueError("failed child requires a non-cancellation failure reason")
        if not isinstance(self.determinism, (OrdinaryReviewFailure, DeterministicReviewFailure)):
            raise ValueError("child failure requires typed deterministic classification")
        if self.reason is ExitReason.TRANSPORT_DEATH and not isinstance(self.determinism, DeterministicReviewFailure):
            raise ValueError("transport death requires positive deterministic classification before a strike")


@dataclass(frozen=True)
class ReviewChildCancelled:
    reason: ReviewCancellationReason
    cancellation_receipt: str

    def __post_init__(self) -> None:
        if self.reason not in (ReviewCancellationReason.OPERATOR, ReviewCancellationReason.SHUTDOWN) or not isinstance(self.reason, ReviewCancellationReason):
            raise ValueError("child cancellation requires operator/shutdown provenance")
        identifier(self.cancellation_receipt, "child cancellation receipt")


@dataclass(frozen=True)
class ReviewChildInfrastructureFailure:
    reason: ExitReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason, ExitReason) or self.reason not in (
            ExitReason.KILLED_EXTERNALLY, ExitReason.RUNTIME_UNAVAILABLE,
        ):
            raise ValueError("review infrastructure failure requires a non-striking exit reason")


@dataclass(frozen=True)
class ReviewChildReturned:
    launched: LaunchedReviewChild
    return_receipt: str
    result: ReviewChildSucceeded | ReviewChildFailed | ReviewChildCancelled | ReviewChildInfrastructureFailure

    def __post_init__(self) -> None:
        if not isinstance(self.launched, LaunchedReviewChild):
            raise ValueError("returned child requires verified launch evidence")
        identifier(self.return_receipt, "settled child return receipt")
        if not isinstance(self.result, (ReviewChildSucceeded, ReviewChildFailed, ReviewChildCancelled, ReviewChildInfrastructureFailure)):
            raise ValueError("child return requires a typed settled result")
        if isinstance(self.result, ReviewChildSucceeded) and isinstance(self.result.session, ReusableReviewSession):
            if self.result.session.source != self.launched:
                raise ValueError("returned session must belong to this exact launched reviewer")

    @property
    def child(self) -> ReviewChildIdentity:
        return self.launched.child


def classify_normal_review_return(
    launched: LaunchedReviewChild, runtime: RuntimeName, envelope: ReviewReturnedEnvelope,
) -> ReviewChildSucceeded | ReviewChildFailed | ReviewChildInfrastructureFailure | None:
    """Interpret a settled normal return; unusual deaths need separate proof."""
    if not isinstance(launched, LaunchedReviewChild) or not isinstance(runtime, RuntimeName) or not isinstance(envelope, ReviewReturnedEnvelope):
        raise ValueError("review return classification requires typed source facts")
    reason = envelope.exit_reason
    if reason is ExitReason.OK and not envelope.is_error:
        session: NoReviewSession | ReusableReviewSession = NoReviewSession()
        capabilities = RUNTIME_CAPABILITIES[runtime]
        if (launched.child.kind is ReviewChildKind.REVIEWER and envelope.session_id
                and capabilities.supports_resume and capabilities.resume_preserves_sandbox):
            try:
                session = ReusableReviewSession(runtime, envelope.session_id, launched)
            except ValueError:
                pass
        return ReviewChildSucceeded(session)
    if envelope.is_error and reason in (ExitReason.KILLED_EXTERNALLY, ExitReason.RUNTIME_UNAVAILABLE):
        return ReviewChildInfrastructureFailure(reason)
    if (envelope.is_error and reason is not None
            and reason not in (
                ExitReason.OK, ExitReason.OPERATOR_CANCELLED, ExitReason.TRANSPORT_DEATH,
                ExitReason.BUDGET_EXHAUSTED,
            )):
        return ReviewChildFailed(reason)
    return None


ReviewChildEvidence = (
    ReviewChildPending | ReviewChildRefused | ReviewChildReleased | ReviewChildSettledWithoutLaunch
    | ReviewChildNotAdmitted | ReviewChildPinDeferred | ReviewChildReturned
)
_CHILD_EVIDENCE = (
    ReviewChildPending, ReviewChildRefused, ReviewChildReleased, ReviewChildSettledWithoutLaunch,
    ReviewChildNotAdmitted, ReviewChildPinDeferred, ReviewChildReturned,
)


@dataclass(frozen=True)
class ReviewSubmissionPending:
    pass


@dataclass(frozen=True)
class ReviewSubmissionConfirmed:
    receipt_id: str

    def __post_init__(self) -> None:
        identifier(self.receipt_id, "confirmed submission receipt")


@dataclass(frozen=True)
class ReviewSubmissionRefused:
    receipt_id: str

    def __post_init__(self) -> None:
        identifier(self.receipt_id, "refused submission receipt")


@dataclass(frozen=True)
class ReviewSubmissionEvidence:
    plan: ReviewSubmissionPlan
    result: ReviewSubmissionPending | ReviewSubmissionConfirmed | ReviewSubmissionRefused

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ReviewSubmissionPlan) or not isinstance(
            self.result, (ReviewSubmissionPending, ReviewSubmissionConfirmed, ReviewSubmissionRefused)
        ):
            raise ValueError("submission evidence requires its exact plan and typed receipt state")


@dataclass(frozen=True)
class ReviewPlanningOpen:
    pass


@dataclass(frozen=True)
class ReviewPlanningComplete:
    pass


@dataclass(frozen=True)
class ReviewPlanningRefused:
    refusal_receipt: str

    def __post_init__(self) -> None:
        identifier(self.refusal_receipt, "round preflight refusal")


ReviewPlanningConclusion = (
    ReviewPlanningComplete | ReviewPlanningRefused | ReviewRoundCancelled
    | ReviewPinDeferred | ReviewPreparationDeferred
)
_CONCLUSIONS = (
    ReviewPlanningComplete, ReviewPlanningRefused, ReviewRoundCancelled,
    ReviewPinDeferred, ReviewPreparationDeferred,
)


@dataclass(frozen=True)
class ReviewPlanningSealed:
    seal_receipt: str
    conclusion: ReviewPlanningConclusion

    def __post_init__(self) -> None:
        identifier(self.seal_receipt, "outer planning seal")
        if not isinstance(self.conclusion, _CONCLUSIONS):
            raise ValueError("planning seal requires its typed branch conclusion")
        if isinstance(self.conclusion, ReviewRoundCancelled) and self.conclusion.reason is ReviewCancellationReason.UNPAID_ABANDONMENT:
            raise ValueError("abandonment requires a verified parent and late-admission fence")


@dataclass(frozen=True)
class ReviewParentAbandoned:
    abandonment_receipt: str
    admission_fence_receipt: str

    def __post_init__(self) -> None:
        identifier(self.abandonment_receipt, "exact parent claim abandonment")
        identifier(self.admission_fence_receipt, "late admission/submission fence")


@dataclass(frozen=True)
class ReviewRoundFacts:
    closure_receipt: str
    plan: ReviewRoundPlan
    planning: ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned
    children: tuple[ReviewChildEvidence, ...]
    submissions: tuple[ReviewSubmissionEvidence, ...]

    def __post_init__(self) -> None:
        identifier(self.closure_receipt, "outer round closure evidence")
        if not isinstance(self.plan, ReviewRoundPlan) or not isinstance(
            self.planning, (ReviewPlanningOpen, ReviewPlanningSealed, ReviewParentAbandoned)
        ):
            raise ValueError("round facts require a plan and typed planning/ownership evidence")
        _tuple(self.children, _CHILD_EVIDENCE, MAX_ROUND_CHILDREN, "child observations")
        _tuple(self.submissions, (ReviewSubmissionEvidence,), MAX_ROUND_SUBMISSIONS, "submission observations")
        if len({child.child.child_id for child in self.children}) != len(self.children):
            raise ValueError("child observations must be coherent, not multiple versions")
        if any(child.child not in self.plan.children for child in self.children):
            raise ValueError("child evidence is outside the immutable plan")
        if len({submission.plan.command_id for submission in self.submissions}) != len(self.submissions):
            raise ValueError("submission observations must be coherent, not multiple versions")
        if any(submission.plan not in self.plan.submissions for submission in self.submissions):
            raise ValueError("submission evidence is outside the immutable plan")
        run_ids = tuple(child.launched.run_id if isinstance(child, ReviewChildReturned) else child.run_id
                        for child in self.children if isinstance(child, (ReviewChildReturned, ReviewChildReleased, ReviewChildSettledWithoutLaunch)))
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("one run cannot establish multiple planned children")
        for child in self.children:
            if isinstance(child, ReviewChildNotAdmitted) and (
                not isinstance(self.planning, ReviewParentAbandoned)
                or child.abandonment_receipt != self.planning.abandonment_receipt
            ):
                raise ValueError("absence of admission requires this exact abandoned parent")
            if isinstance(child, ReviewChildReturned) and isinstance(child.result, ReviewChildSucceeded) and isinstance(child.result.session, ReusableReviewSession):
                if child.result.session.runtime is not self.plan.runtime:
                    raise ValueError("reviewer session runtime differs from the captured plan")


class ReviewRoundWait(str, Enum):
    PLANNING = "planning"
    CHILDREN = "children"
    SUBMISSIONS = "submissions"


@dataclass(frozen=True)
class ReviewRoundAwaitingEvidence:
    reasons: frozenset[ReviewRoundWait]

    def __post_init__(self) -> None:
        if not isinstance(self.reasons, frozenset) or not self.reasons or any(not isinstance(reason, ReviewRoundWait) for reason in self.reasons):
            raise ValueError("an unfinished round requires typed waiting reasons")


@dataclass(frozen=True)
class FinalizedReviewRound:
    outcome: ReviewRoundOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReviewRoundOutcome):
            raise ValueError("finalization requires a validated outer outcome")


@dataclass(frozen=True)
class DeferredReviewPreparation:
    """Commit the outcome and existing bounded evidence scheduling together."""

    outcome: ReviewRoundOutcome
    failures: tuple[ReviewPreparationFailure, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReviewRoundOutcome) or isinstance(
            self.outcome.disposition, (PaidReviewProgress, CopiedReviewProgress)
        ):
            raise ValueError("preparation scheduling requires a nonproductive outer outcome")
        _tuple(self.failures, (ReviewPreparationFailure,), MAX_ROUND_CHILDREN + 1, "preparation failures")
        if not self.failures or len(set(self.failures)) != len(self.failures):
            raise ValueError("preparation deferral requires distinct failure evidence")


def finalize_review_round(
    facts: ReviewRoundFacts,
) -> ReviewRoundAwaitingEvidence | FinalizedReviewRound | DeferredReviewPreparation:
    """Classify one sealed round; a missing receipt never proves a failed run.

    The adapter must retain these complete closure facts with the returned
    outcome in the same transaction. The closure receipt is the reference for
    compound pin, deterministic-failure and mixed unlaunched evidence; choosing
    one arbitrary child's receipt would lose the siblings' provenance.
    """
    if not isinstance(facts, ReviewRoundFacts):
        raise ValueError("round finalization requires validated evidence")
    plan = facts.plan
    confirmed = tuple(submission for submission in facts.submissions if isinstance(submission.result, ReviewSubmissionConfirmed))
    if len(confirmed) > 1:
        raise ValueError("one outer round cannot establish multiple productive submissions")
    waiting = frozenset(
        reason for condition, reason in (
            (isinstance(facts.planning, ReviewPlanningOpen), ReviewRoundWait.PLANNING),
            (len(facts.children) != len(plan.children) or any(isinstance(child, ReviewChildPending) for child in facts.children), ReviewRoundWait.CHILDREN),
            (len(facts.submissions) != len(plan.submissions) or any(isinstance(submission.result, ReviewSubmissionPending) for submission in facts.submissions), ReviewRoundWait.SUBMISSIONS),
        ) if condition
    )
    if waiting:
        return ReviewRoundAwaitingEvidence(waiting)
    returned = tuple(sorted((child for child in facts.children if isinstance(child, ReviewChildReturned)), key=lambda child: child.child.child_id))
    launched = tuple(child.launched for child in returned)
    conclusion = facts.planning.conclusion if isinstance(facts.planning, ReviewPlanningSealed) else None
    if confirmed:
        submission = confirmed[0]
        assert isinstance(submission.result, ReviewSubmissionConfirmed)
        receipt = submission.result.receipt_id
        if submission.plan.kind is ReviewSubmissionKind.COPIED:
            if launched:
                raise ValueError("copied-verdict progress cannot conceal launched fallback work")
            return FinalizedReviewRound(ReviewRoundOutcome(plan.round, CopiedReviewProgress(receipt)))
        if not launched:
            raise ValueError("paid productive submission requires actual launch evidence")
        session = PreserveReviewSession()
        reviewer = next((child for child in returned if child.child.kind is ReviewChildKind.REVIEWER
                         and isinstance(child.result, ReviewChildSucceeded)), None)
        if reviewer is not None:
            assert isinstance(reviewer.result, ReviewChildSucceeded)
            session = (ReplaceReviewSession(reviewer.result.session)
                       if plan.session_reuse is ReviewSessionReuse.REUSABLE and isinstance(reviewer.result.session, ReusableReviewSession)
                       else ClearReviewSession(reviewer.launched))
        return FinalizedReviewRound(ReviewRoundOutcome(
            plan.round, PaidReviewProgress(receipt, launched), session, SetDisplayedRound(plan.displayed_round),
        ))
    preparation = tuple(sorted({
        *(child.preparation for child in facts.children if isinstance(child, ReviewChildSettledWithoutLaunch)),
        *((conclusion.failure,) if isinstance(conclusion, ReviewPreparationDeferred) else ()),
    }, key=lambda failure: (failure.kind.value, failure.evidence_id)))

    def finished(outcome: ReviewRoundOutcome) -> FinalizedReviewRound | DeferredReviewPreparation:
        return DeferredReviewPreparation(outcome, preparation) if preparation else FinalizedReviewRound(outcome)

    cancellation = conclusion if isinstance(conclusion, ReviewRoundCancelled) else None
    if cancellation is None:
        cancelled = tuple(child.result for child in returned if isinstance(child.result, ReviewChildCancelled))
        if cancelled:
            reason = (ReviewCancellationReason.SHUTDOWN if any(item.reason is ReviewCancellationReason.SHUTDOWN for item in cancelled)
                      else ReviewCancellationReason.OPERATOR)
            cancellation = ReviewRoundCancelled(reason, facts.closure_receipt)
    if cancellation is not None:
        return finished(ReviewRoundOutcome(plan.round, cancellation))
    if isinstance(conclusion, ReviewPinDeferred) or any(isinstance(child, ReviewChildPinDeferred) for child in facts.children):
        return finished(ReviewRoundOutcome(plan.round, ReviewPinDeferred(facts.closure_receipt)))
    if launched:
        failed = tuple(child for child in returned if isinstance(child.result, ReviewChildFailed))
        refused_submission = any(isinstance(submission.result, ReviewSubmissionRefused)
                                 for submission in facts.submissions)
        if (failed or refused_submission
                or not any(isinstance(child.result, ReviewChildInfrastructureFailure) for child in returned)):
            deterministic = any(isinstance(child.result.determinism, DeterministicReviewFailure)
                                for child in failed)
            debit = SaturateReviewFailures(plan.failure_ceiling, facts.closure_receipt) if deterministic and plan.failure_ceiling else AddReviewStrike()
            return finished(ReviewRoundOutcome(plan.round, ReviewRoundFailure(launched, facts.closure_receipt, debit)))
        return finished(ReviewRoundOutcome(plan.round, ReviewInfrastructureDeath(launched, facts.closure_receipt)))
    if preparation:
        disposition = conclusion if isinstance(conclusion, ReviewPreparationDeferred) and not facts.children else ReviewUnlaunchedRound(facts.closure_receipt)
        return DeferredReviewPreparation(ReviewRoundOutcome(plan.round, disposition), preparation)
    if isinstance(conclusion, ReviewPlanningRefused) and not facts.children:
        return FinalizedReviewRound(ReviewRoundOutcome(plan.round, ReviewAdmissionRefused(conclusion.refusal_receipt)))
    if isinstance(facts.planning, ReviewParentAbandoned) and all(isinstance(child, ReviewChildNotAdmitted) for child in facts.children) and not facts.submissions:
        return FinalizedReviewRound(ReviewRoundOutcome(
            plan.round, ReviewRoundCancelled(ReviewCancellationReason.UNPAID_ABANDONMENT, facts.closure_receipt),
        ))
    if facts.children or facts.submissions:
        return FinalizedReviewRound(ReviewRoundOutcome(plan.round, ReviewUnlaunchedRound(facts.closure_receipt)))
    raise ValueError("an empty finished round requires explicit refusal, preparation or abandonment evidence")
