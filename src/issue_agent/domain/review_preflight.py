"""Pure selection of one execution round under captured review authority."""

from dataclasses import dataclass
from datetime import datetime

from .agent_task import RUNTIME_CAPABILITIES, RuntimeName
from .labels import Label
from .review import ReviewMode, labels_holding_review
from .review_execution import (
    MAX_EXECUTION_SUFFIX, CurrentReviewExecution, ReusableReviewSession,
    ReviewExecutionBinding, ReviewRoundIdentity, compatible_review_session,
)
from .review_rounds import ReviewRoundPlan, ReviewSessionReuse
from .review_workflow import (
    CompleteReviews, ObservedPullRequest, PendingReviewRearm, PullRequestState,
    ReviewedBaseline, ReviewWorkflowSnapshot,
)
from .workflow_identity import (
    WorkflowRevision, validate_workflow_identifier as identifier,
    validate_workflow_integer as integer,
)

MAX_REVIEW_PREFLIGHT_AGE_SECONDS = 300


@dataclass(frozen=True)
class ReviewPreflightSelection:
    request_id: str
    expected: WorkflowRevision
    binding: ReviewExecutionBinding
    claim_occurrence_id: str
    claim_target: int
    observation: ObservedPullRequest
    trigger_label: Label
    mode: ReviewMode
    runtime: RuntimeName
    failure_ceiling: int

    def __post_init__(self) -> None:
        identifier(self.request_id, "review preflight request")
        if not isinstance(self.expected, WorkflowRevision) or not isinstance(self.binding, ReviewExecutionBinding):
            raise ValueError("review preflight requires its aggregate revision and dual binding")
        if self.expected.fence != self.binding.rejection.fence:
            raise ValueError("review preflight revision disagrees with its captured binding")
        identifier(self.claim_occurrence_id, "review parent claim occurrence")
        if type(self.claim_target) is not int or self.claim_target == 0 or abs(self.claim_target) > 2**63 - 1:
            raise ValueError("review preflight requires a bounded signed claim target")
        if self.claim_target < 0 and -self.claim_target != self.expected.fence.key.target.number:
            raise ValueError("unlinked review claim must name the selected pull request")
        if not isinstance(self.observation, ObservedPullRequest) or self.observation.key != self.expected.fence.key:
            raise ValueError("review preflight observation belongs to another pull request")
        if not isinstance(self.trigger_label, Label) or not isinstance(self.mode, ReviewMode) or not isinstance(self.runtime, RuntimeName):
            raise ValueError("review preflight requires captured trigger, mode and runtime")
        integer(self.failure_ceiling, "review failure ceiling")


def require_review_execution_eligibility(
    snapshot: ReviewWorkflowSnapshot, head_sha: str, failure_ceiling: int,
) -> None:
    """Current paid-work allowance for an already selected review head."""
    if not isinstance(snapshot.execution, CurrentReviewExecution):
        raise ValueError("review execution activation is required")
    if snapshot.gates or isinstance(snapshot.intent, PendingReviewRearm):
        raise ValueError("review execution is held by workflow recovery")
    if failure_ceiling and snapshot.execution.state.failures >= failure_ceiling:
        raise ValueError("the review execution allowance is exhausted")
    continuation = snapshot.episode.continuation
    if isinstance(continuation, ReviewedBaseline) and continuation.head_sha == head_sha:
        raise ValueError("the current review head already has a settled baseline")


def plan_review_preflight(
    snapshot: ReviewWorkflowSnapshot, selection: ReviewPreflightSelection, *, now: datetime,
) -> ReviewRoundPlan:
    """Select a free preflight; the adapter verifies claim/observation provenance."""
    if not isinstance(snapshot, ReviewWorkflowSnapshot) or not isinstance(selection, ReviewPreflightSelection):
        raise ValueError("review preflight requires typed authority and selection")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("review preflight requires an aware authority clock")
    if not isinstance(snapshot.execution, CurrentReviewExecution):
        raise ValueError("review execution activation is required")
    if snapshot.revision != selection.expected or snapshot.execution.binding != selection.binding:
        raise ValueError("review preflight captured a stale aggregate revision or execution binding")
    observed = selection.observation
    if observed.state is not PullRequestState.OPEN or not isinstance(observed.reviews, CompleteReviews):
        raise ValueError("review preflight requires an open pull request and complete reviews")
    if not 0 <= (now - observed.observed_at).total_seconds() <= MAX_REVIEW_PREFLIGHT_AGE_SECONDS:
        raise ValueError("review preflight observation is stale or from the future")
    if selection.trigger_label not in observed.labels or labels_holding_review(observed.labels):
        raise ValueError("review preflight trigger is absent or the pull request is held")
    require_review_execution_eligibility(snapshot, observed.head_sha, selection.failure_ceiling)
    state = snapshot.execution.state
    if snapshot.execution.last_sequence - snapshot.execution.baseline.through_sequence >= MAX_EXECUTION_SUFFIX:
        raise ValueError("review execution unresolved pending capacity is exhausted")
    session = compatible_review_session(state.session, selection.runtime, RUNTIME_CAPABILITIES[selection.runtime])
    reuse = ReviewSessionReuse.REUSABLE if isinstance(session, ReusableReviewSession) else ReviewSessionReuse.FRESH
    integer(snapshot.execution.last_sequence + 1, "next review round sequence", minimum=1)
    integer(state.displayed_round + 1, "next displayed review round", minimum=1)
    return ReviewRoundPlan(
        ReviewRoundIdentity(selection.binding, selection.request_id, snapshot.execution.last_sequence + 1),
        observed.head_sha, selection.mode, selection.runtime, reuse,
        state.displayed_round + 1, selection.failure_ceiling,
    )
