"""Review round selection requires current, complete and bounded authority."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.labels import Label
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.review_execution import (
    CurrentReviewExecution, ExecutionNotActivated, ReviewExecutionBinding, ReviewExecutionCheckpoint,
    ReviewExecutionOrigin, ReviewExecutionOriginKind, ReviewExecutionState,
)
from issue_agent.domain.review_preflight import ReviewPreflightSelection, plan_review_preflight
from issue_agent.domain.review_rounds import ReviewSessionReuse
from issue_agent.domain.review_workflow import (
    CompleteReviews, FreshReviewRequired, ObservedPullRequest, PartialReviews,
    PullRequestState, ReviewEpisode, ReviewWorkflowSnapshot,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence,
    WorkflowKey, WorkflowRevision,
)

NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)
KEY = WorkflowKey("o/r", PullRequestRef(9))


def active():
    revision = WorkflowRevision(WorkflowFence(KEY, 0), 1)
    authority = FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1)
    episode = ReviewEpisode("review:1", continuation=FreshReviewRequired(0))
    binding = ReviewExecutionBinding(WorkflowBinding(revision.fence, authority, episode.episode_id), 1, "execution:1")
    state = ReviewExecutionState(binding)
    execution = CurrentReviewExecution(
        ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin:1"),
        ReviewExecutionCheckpoint(0, state), state, 0, 0,
    )
    snapshot = ReviewWorkflowSnapshot(revision, authority, episode, execution=execution)
    observed = ObservedPullRequest(KEY, "a" * 40, PullRequestState.OPEN,
                                   frozenset({Label.AI_PR_OPEN}), CompleteReviews(()), NOW)
    selection = ReviewPreflightSelection("round:1", revision, binding, "claim:1", -9,
                                         observed, Label.AI_PR_OPEN, ReviewMode.SINGLE, RuntimeName.CLAUDE, 5)
    return snapshot, selection


def exhausted(snapshot, selection):
    state = replace(snapshot.execution.state, failures=5)
    from issue_agent.domain.review_execution import CurrentReviewExecution
    assert isinstance(snapshot.execution, CurrentReviewExecution)
    execution = replace(snapshot.execution, baseline=replace(snapshot.execution.baseline, state=state), state=state)
    return replace(snapshot, execution=execution), selection


def test_preflight_allocates_one_unpaid_round_from_current_snapshot():
    snapshot, selection = active()
    plan = plan_review_preflight(snapshot, selection, now=NOW)
    assert plan.round.sequence == 1
    assert plan.round.round_id == selection.request_id
    assert plan.head_sha == selection.observation.head_sha
    assert plan.displayed_round == 1
    assert plan.session_reuse is ReviewSessionReuse.FRESH
    assert plan.children == () and plan.submissions == ()
    assert snapshot.execution.last_sequence == 0


@pytest.mark.parametrize("runtime,expected", [(RuntimeName.CLAUDE, ReviewSessionReuse.REUSABLE),
                                            (RuntimeName.GLM, ReviewSessionReuse.FRESH),
                                            (RuntimeName.CODEX, ReviewSessionReuse.FRESH)])
def test_current_session_reuse_requires_same_runtime_and_preserved_sandbox(runtime, expected):
    from issue_agent.domain.review_execution import ImportedReviewSession, ReusableReviewSession

    snapshot, selection = active()
    state = replace(snapshot.execution.state,
                    session=ReusableReviewSession(RuntimeName.CLAUDE, "old-compatible-session", ImportedReviewSession("import:verified")))
    from issue_agent.domain.review_execution import CurrentReviewExecution
    assert isinstance(snapshot.execution, CurrentReviewExecution)
    execution = replace(snapshot.execution, baseline=replace(snapshot.execution.baseline, state=state), state=state)
    snapshot = replace(snapshot, execution=execution)
    plan = plan_review_preflight(snapshot, replace(selection, runtime=runtime), now=NOW)
    assert plan.session_reuse is expected


@pytest.mark.parametrize("change", [
    lambda s, r: (replace(s, revision=replace(s.revision, version=2)), r),
    lambda s, r: (s, replace(r, observation=replace(r.observation, reviews=PartialReviews("truncated")))),
    lambda s, r: (s, replace(r, observation=replace(r.observation, labels=frozenset({Label.NEEDS_HUMAN_REVIEW})))),
    lambda s, r: (s, replace(r, observation=replace(r.observation, state=PullRequestState.CLOSED))),
    lambda s, r: (s, replace(r, observation=replace(r.observation, observed_at=NOW - timedelta(seconds=301)))),
    exhausted,
])
def test_preflight_refuses_stale_incomplete_held_or_exhausted_evidence(change):
    snapshot, selection = change(*active())
    with pytest.raises(ValueError):
        plan_review_preflight(snapshot, selection, now=NOW)


def test_preflight_refuses_invalid_claim_identity_and_inactive_execution():
    snapshot, selection = active()
    with pytest.raises(ValueError, match="unlinked"):
        replace(selection, claim_target=-10)
    with pytest.raises(ValueError, match="activation"):
        plan_review_preflight(replace(snapshot, execution=ExecutionNotActivated()), selection, now=NOW)


def test_preflight_distinguishes_blocked_migration_from_missing_activation():
    from issue_agent.domain.review_execution_migration import (
        BlockedReviewExecutionMigration, ReviewImportBlocker, ReviewImportBlockerKind,
        ReviewImportHistoryBlocked, ReviewImportSource,
    )
    snapshot, selection = active()
    key = snapshot.revision.fence.key
    blocked = BlockedReviewExecutionMigration(key, FamilyAuthority(WorkflowFamily.REVIEW_EXECUTION, 2),
        ReviewImportHistoryBlocked(key.target.number, (ReviewImportBlocker(ReviewImportBlockerKind.ORIGIN,
            (ReviewImportSource("round", "retained-round", "a" * 64),)),)), "b" * 64, "c" * 64)
    with pytest.raises(ValueError, match="migration is blocked"):
        plan_review_preflight(replace(snapshot, execution=blocked), selection, now=NOW)


def test_historical_pr_repair_hold_refuses_review_despite_current_execution():
    from issue_agent.domain.continuation_components import BlockedPRRepair, ContinuationImportBlocker

    snapshot, selection = active()
    blocked = BlockedPRRepair(KEY, FamilyAuthority(WorkflowFamily.PR_REPAIR, 1), "historical-import",
                              (ContinuationImportBlocker("run:old", "unverified repair chronology"),))
    with pytest.raises(ValueError, match="historical PR repair migration is blocked"):
        plan_review_preflight(replace(snapshot, pr_repair=blocked), selection, now=NOW)
