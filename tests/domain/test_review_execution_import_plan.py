"""An empty inherited history needs a current, complete PR before import."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.domain.labels import Label
from issue_agent.domain.review_activation import OpenPullRequestsListed
from issue_agent.domain.review_execution import ReviewExecutionOriginKind
from issue_agent.domain.review_execution_counter_mapping import LegacyOpenPRCounterLink
from issue_agent.domain.review_execution_history_import import ReviewImportNoHistoryObserved
from issue_agent.domain.review_execution_import_plan import (
    BlockedZeroHistoryReviewImport, EligibleZeroHistoryReviewImport,
    EmptyReviewExecutionImportPlan,
    ZeroHistoryImportBlockerKind, propose_zero_history_review_execution,
    screen_zero_history_review_import,
)
from issue_agent.domain.review_workflow import (
    CompleteReviews, ImportedBaseline, ObservedPullRequest, PartialReviews,
    PullRequestState, RecordedPullRequest, ReviewedBaseline, SubmittedReviewFact,
    SubmittedReviewState,
)
from issue_agent.domain.review_workflow_policy import initial_review_workflow
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowFamily, WorkflowKey,
)


NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
HEAD = "a" * 40
KEY = WorkflowKey("o/r", PullRequestRef(9))
BOT = "review-bot[bot]"


def _facts():
    history = ReviewImportNoHistoryObserved(9, "b" * 64)
    workflow = initial_review_workflow(KEY, FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1))
    observation = RecordedPullRequest(ObservedPullRequest(
        KEY, HEAD, PullRequestState.OPEN, frozenset({Label.AI_PR_OPEN}), CompleteReviews(()), NOW,
    ))
    link = LegacyOpenPRCounterLink(9, HEAD, "work/topic", None, None)
    return history, workflow, observation, link


def _screen(facts):
    return screen_zero_history_review_import(facts[0], facts[1], facts[2], facts[3], BOT, now=NOW)


def test_empty_existing_pr_proposes_deterministic_imported_zero_origin():
    eligible = _screen(_facts())

    assert isinstance(eligible, EligibleZeroHistoryReviewImport)
    decision = propose_zero_history_review_execution(eligible, 2, "c" * 64, "d" * 64, now=NOW)
    assert decision == propose_zero_history_review_execution(eligible, 2, "c" * 64, "d" * 64, now=NOW)
    assert decision.origin.kind is ReviewExecutionOriginKind.IMPORTED
    assert decision.checkpoint.through_sequence == 0
    assert decision.checkpoint.state.failures == 0
    assert decision.checkpoint.state.displayed_round == 0
    assert decision.checkpoint.state.binding.rejection == eligible.workflow.binding
    assert decision.checkpoint.state.binding.execution_epoch == 2
    assert decision.source_digest == "d" * 64
    assert decision.origin != propose_zero_history_review_execution(eligible, 3, "c" * 64, "d" * 64, now=NOW).origin


@pytest.mark.parametrize("change,kind", (
    ("partial_reviews", ZeroHistoryImportBlockerKind.REMOTE_UNAVAILABLE),
    ("closed", ZeroHistoryImportBlockerKind.REMOTE_UNAVAILABLE),
    ("changed_head", ZeroHistoryImportBlockerKind.CHANGED_HEAD),
    ("bot_review", ZeroHistoryImportBlockerKind.PRIOR_REVIEW),
    ("human_marker", ZeroHistoryImportBlockerKind.PRIOR_REVIEW),
    ("reviewed_baseline", ZeroHistoryImportBlockerKind.WORKFLOW_HISTORY),
    ("advanced_generation", ZeroHistoryImportBlockerKind.WORKFLOW_HISTORY),
    ("advanced_version", ZeroHistoryImportBlockerKind.WORKFLOW_HISTORY),
    ("verdict_label", ZeroHistoryImportBlockerKind.VERDICT_HOLD),
    ("external_hold", ZeroHistoryImportBlockerKind.EXTERNAL_HOLD),
    ("different_pr", ZeroHistoryImportBlockerKind.IDENTITY),
))
def test_zero_history_proposal_refuses_conflicting_current_facts(change, kind):
    history, workflow, recorded, link = _facts()
    observed = recorded.observation
    review = SubmittedReviewFact("review-1", BOT, SubmittedReviewState.COMMENTED, HEAD, NOW)
    if change == "partial_reviews":
        recorded = replace(recorded, observation=replace(observed, reviews=PartialReviews("page failed")))
    elif change == "closed":
        recorded = replace(recorded, observation=replace(observed, state=PullRequestState.CLOSED))
    elif change == "changed_head":
        recorded = replace(recorded, observation=replace(observed, head_sha="e" * 40))
    elif change == "bot_review":
        recorded = replace(recorded, observation=replace(observed, reviews=CompleteReviews((review,))))
    elif change == "human_marker":
        review = replace(review, author="human", review_command="claimed-command")
        recorded = replace(recorded, observation=replace(observed, reviews=CompleteReviews((review,))))
    elif change == "reviewed_baseline":
        continuation = ReviewedBaseline(HEAD, "d" * 64, 0, ImportedBaseline("old-review"))
        workflow = replace(workflow, episode=replace(workflow.episode, continuation=continuation))
    elif change == "advanced_generation":
        workflow = replace(workflow, revision=replace(
            workflow.revision, fence=replace(workflow.revision.fence, generation=1),
        ))
    elif change == "advanced_version":
        workflow = replace(workflow, revision=replace(workflow.revision, version=1))
    elif change == "verdict_label":
        recorded = replace(recorded, observation=replace(
            observed, labels=frozenset({Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW}),
        ))
    elif change == "external_hold":
        recorded = replace(recorded, external_holds=("issue:held",))
    else:
        link = LegacyOpenPRCounterLink(10, HEAD, "work/topic", None, None)

    result = _screen((history, workflow, recorded, link))

    assert result == BlockedZeroHistoryReviewImport(9, kind)


def test_proposal_digest_changes_with_full_remote_and_workflow_facts():
    history, workflow, recorded, link = _facts()
    first = _screen((history, workflow, recorded, link))
    changed_labels = _screen((history, workflow, replace(recorded, observation=replace(
        recorded.observation, labels=frozenset({Label.AI_PR_OPEN, Label.RECURRENCE}),
    )), link))
    changed_workflow = _screen((history, replace(workflow, episode=replace(workflow.episode, guidance="check")),
                                recorded, link))

    assert isinstance(first, EligibleZeroHistoryReviewImport)
    assert isinstance(changed_labels, EligibleZeroHistoryReviewImport)
    assert isinstance(changed_workflow, EligibleZeroHistoryReviewImport)
    assert len({first.source_digest, changed_labels.source_digest, changed_workflow.source_digest}) == 3
    with pytest.raises(ValueError, match="source facts"):
        replace(first, source_digest="f" * 64)


def test_proposal_refuses_untyped_and_malformed_plan_inputs():
    eligible = _screen(_facts())
    assert isinstance(eligible, EligibleZeroHistoryReviewImport)
    with pytest.raises(ValueError, match="typed observations"):
        screen_zero_history_review_import(None, *_facts()[1:], BOT, now=NOW)
    with pytest.raises(ValueError, match="SHA-256"):
        propose_zero_history_review_execution(eligible, 2, "bad", "d" * 64, now=NOW)
    with pytest.raises(ValueError, match="execution import epoch"):
        propose_zero_history_review_execution(eligible, 0, "c" * 64, "d" * 64, now=NOW)


def test_stale_or_future_remote_observation_cannot_propose_an_import():
    facts = _facts()
    stale = screen_zero_history_review_import(facts[0], facts[1], facts[2], facts[3], BOT,
                                              now=NOW.replace(hour=1))
    future = screen_zero_history_review_import(facts[0], facts[1], facts[2], facts[3], BOT,
                                               now=NOW.replace(day=29))
    assert stale == BlockedZeroHistoryReviewImport(9, ZeroHistoryImportBlockerKind.STALE_OBSERVATION)
    assert future == BlockedZeroHistoryReviewImport(9, ZeroHistoryImportBlockerKind.STALE_OBSERVATION)
    eligible = _screen(facts)
    assert isinstance(eligible, EligibleZeroHistoryReviewImport)
    with pytest.raises(ValueError, match="fresh current evidence"):
        propose_zero_history_review_execution(eligible, 1, "c" * 64, "d" * 64,
                                               now=NOW.replace(hour=1))


def test_whole_repository_plan_binds_every_pr_and_avoids_decision_digest_cycle():
    eligible = _screen(_facts())
    assert isinstance(eligible, EligibleZeroHistoryReviewImport)
    plan = EmptyReviewExecutionImportPlan(
        "o/r", OpenPullRequestsListed((9,)), 2,
        eligible.history.source_digest, "c" * 64, "d" * 64, (eligible,), NOW,
    )

    decision, = plan.decisions_at(NOW)
    assert decision.activation_plan_digest == plan.plan_digest
    assert decision.source_digest == plan.source_digest
    assert decision.checkpoint.state.binding.execution_epoch == 2
    assert plan == replace(plan)
    assert replace(plan, inventory_digest="e" * 64).plan_digest != plan.plan_digest
    with pytest.raises(ValueError, match="fresh current evidence"):
        plan.decisions_at(NOW.replace(hour=1))
    with pytest.raises(ValueError, match="current aware time"):
        plan.decisions_at(NOW.replace(day=29))
    with pytest.raises(ValueError, match="every listed PR"):
        replace(plan, listing=OpenPullRequestsListed((9, 10)))
    with pytest.raises(ValueError, match="every listed PR"):
        replace(plan, source_set_digest="f" * 64)


def test_fresh_reread_preserves_reviewed_plan_identity_when_facts_are_unchanged():
    facts = _facts()
    first = _screen(facts)
    second_time = NOW.replace(second=20)
    second_observation = replace(facts[2], observation=replace(
        facts[2].observation, observed_at=second_time,
    ))
    second = screen_zero_history_review_import(
        facts[0], facts[1], second_observation, facts[3], BOT, now=second_time,
    )

    assert isinstance(first, EligibleZeroHistoryReviewImport)
    assert isinstance(second, EligibleZeroHistoryReviewImport)
    assert first.source_digest == second.source_digest
    original = EmptyReviewExecutionImportPlan(
        "o/r", OpenPullRequestsListed((9,)), 2,
        first.history.source_digest, "c" * 64, "d" * 64, (first,), NOW,
    )
    reread = replace(original, candidates=(second,), planned_at=second_time)
    assert original.source_digest == reread.source_digest
    assert original.plan_digest == reread.plan_digest
    assert original.decisions_at(second_time) == reread.decisions_at(second_time)


def test_remote_review_order_does_not_change_source_identity():
    history, workflow, recorded, link = _facts()
    reviews = (
        SubmittedReviewFact("review-b", "human-b", SubmittedReviewState.COMMENTED, HEAD, NOW),
        SubmittedReviewFact("review-a", "human-a", SubmittedReviewState.APPROVED, HEAD, NOW),
    )
    first_observation = replace(recorded, observation=replace(
        recorded.observation, reviews=CompleteReviews(reviews),
    ))
    second_observation = replace(recorded, observation=replace(
        recorded.observation, reviews=CompleteReviews(tuple(reversed(reviews))),
    ))

    first = _screen((history, workflow, first_observation, link))
    second = _screen((history, workflow, second_observation, link))

    assert isinstance(first, EligibleZeroHistoryReviewImport)
    assert isinstance(second, EligibleZeroHistoryReviewImport)
    assert first.source_digest == second.source_digest
