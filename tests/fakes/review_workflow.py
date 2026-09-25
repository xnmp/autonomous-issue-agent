"""Explicit review-rejection workflow fixtures for isolated tests, never an importer."""

from datetime import datetime, timedelta, timezone

from issue_agent.domain.contracts import CommandId
from issue_agent.domain.labels import Label
from issue_agent.domain.review_workflow import (
    CancelledReviewRearm, CompletedReviewRearm, CountedReview, DismissCountedReview, EnsureReviewProjection,
    FreshReviewRequired, PendingReviewRearm, PullRequestState, RequireFreshReview, ReviewEpisode, ReviewRearmPreflight,
    ReviewRejectionCeiling, ReviewWorkflowSnapshot, SupersededReviewRearm,
)
from issue_agent.domain.workflow import (
    KeepGuidance, NoAuthorizedRemediation, OpenGate, OperatorActor, RearmingGate, RequestRearm, StopReason,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, PullRequestRef, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
PATCH = "c" * 64
PR = PullRequestRef(9)
PR_KEY = WorkflowKey("o/r", PR)
ISSUE_KEY = WorkflowKey("o/r", IssueRef(7))
REVIEW = FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1)
RESOLVE = FamilyAuthority(WorkflowFamily.RESOLVE, 1)
ACTOR = OperatorActor("operator:alice", "dashboard")
EPISODE = "review-episode"
GATE = "gate:review-ceiling"


def revision(generation=0, version=0, key=PR_KEY):
    return WorkflowRevision(WorkflowFence(key, generation), version)


def counted(count=2, *, commit=HEAD, prefix="review"):
    return tuple(CountedReview(f"{prefix}-{index}", commit) for index in range(count))


def ceiling_cause(count=2, ceiling=3, *, episode=EPISODE):
    return ReviewRejectionCeiling(episode, HEAD, "review-bot[bot]", counted(count), ceiling, ())


def ceiling_gate(generation=0, cause=None):
    return OpenGate(GATE, generation, cause or ceiling_cause(),
                    NoAuthorizedRemediation("a counted-review dismissal needs an operator decision"))


def held(*, generation=0, version=0, key=PR_KEY):
    return ReviewWorkflowSnapshot(revision(generation, version, key), REVIEW, ReviewEpisode(EPISODE), (ceiling_gate(),))


def operations(pr=PR, dismissals=None):
    reviews = counted() if dismissals is None else dismissals
    return tuple(DismissCountedReview(pr, review) for review in reviews) + (RequireFreshReview(pr), EnsureReviewProjection(pr))


def pending_intent(request_id="request:1", *, from_generation=0, ops=None, commands=None, authorized=None):
    return PendingReviewRearm(
        request_id, GATE, from_generation, from_generation + 1, ops if ops is not None else operations(),
        commands if commands is not None else (CommandId(f"{request_id}:dismiss-0"), CommandId(f"{request_id}:dismiss-1"),
                                               CommandId(f"{request_id}:projection")),
        counted() if authorized is None else authorized,
    )


def pending(request_id="request:1"):
    intent = pending_intent(request_id)
    return ReviewWorkflowSnapshot(
        revision(1, 1), REVIEW, ReviewEpisode(EPISODE, "fix the flaky test", FreshReviewRequired(1)),
        (RearmingGate(ceiling_gate(), request_id),), intent,
    )


def request(before=None, request_id="request:1"):
    before = before or held()
    return RequestRearm(request_id, before.revision, GATE, KeepGuidance())


def preflight(key=PR_KEY, reviews=None, *, head=HEAD):
    return ReviewRearmPreflight(key, head, "complete-reviews:1", counted() if reviews is None else reviews,
                                PullRequestState.OPEN, frozenset({Label.NEEDS_HUMAN_REVIEW}))


def completed(request_id="request:1"):
    state = pending(request_id)
    intent = state.intent
    assert isinstance(intent, PendingReviewRearm)
    return ReviewWorkflowSnapshot(
        revision(1, 2), REVIEW, state.episode, (),
        CompletedReviewRearm(request_id, GATE, 1, intent.required_commands),
    )


def cancelled(request_id="request:1"):
    state = pending(request_id)
    intent = state.intent
    assert isinstance(intent, PendingReviewRearm)
    return ReviewWorkflowSnapshot(
        revision(1, 2), REVIEW, state.episode, (),
        CancelledReviewRearm(request_id, GATE, 1, "cancel:1", "scope changed", (), intent.required_commands, intent.authorized),
    )


def superseded(request_id="request:1"):
    state = pending(request_id)
    intent = state.intent
    assert isinstance(intent, PendingReviewRearm)
    return ReviewWorkflowSnapshot(
        revision(1, 2), REVIEW, state.episode, (),
        SupersededReviewRearm(request_id, GATE, 1, StopReason.DECISION_CHANGED, "head-moved:1",
                              intent.required_commands[:1], intent.required_commands[1:], intent.authorized),
    )


def later(seconds):
    return NOW + timedelta(seconds=seconds)
