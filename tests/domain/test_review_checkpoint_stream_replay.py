"""Incremental checkpoint replay contracts over finalized source order."""

from dataclasses import replace

import pytest

from issue_agent.domain.review_checkpoint_application import (
    ApplyAllCheckpointTailOutcomes, ApplyCheckpointTailByFollowingBoundary,
    ReviewCheckpointApplicationProposal,
    build_review_checkpoint_application_proposal,
    replay_review_checkpoint_application,
)
from issue_agent.domain.review_checkpoint_stream_replay import (
    _CheckpointReplayBetweenBoundaries,
    advance_checkpoint_replay_boundary_round, advance_checkpoint_replay_tail_round,
    begin_checkpoint_stream_replay, finish_checkpoint_replay_boundary,
    finish_checkpoint_stream_replay, start_checkpoint_replay_boundary,
    start_checkpoint_replay_tail,
)
from issue_agent.domain.review_execution import (
    MAX_EXECUTION_SUFFIX, ActivatedReviewExecution, ReviewExecutionBoundaryCursor,
    ReviewExecutionCheckpoint, ReviewExecutionOrigin, ReviewExecutionOriginKind,
    ReviewGenerationArchiveFingerprint, carry_review_execution_generation,
    plan_review_execution_compaction, project_review_execution,
)
from issue_agent.domain.workflow_identity import WorkflowRevision
from tests.domain.test_review_execution import (
    binding, checkpoint, failed, generation_boundary,
)


def test_selected_tail_stream_replay_skips_unapplied_finalized_round():
    initial = checkpoint()
    outcomes = (failed(1), failed(2))
    before = project_review_execution(initial, (outcomes[1],)).state
    after = carry_review_execution_generation(before, binding(1), 3)
    boundary = generation_boundary(before, after, 3, (2,), "boundary:stream-selected")
    execution = ActivatedReviewExecution(
        ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin:stream-selected"),
        initial, after, 3, boundaries=(boundary,),
    )
    plan = plan_review_execution_compaction(execution, outcomes, 2)
    source = ReviewGenerationArchiveFingerprint(
        ReviewExecutionBoundaryCursor(boundary.receipt_id, boundary.to_revision),
        "a" * 64, "workflow-request", "request:stream-selected", "b" * 64,
    )
    application = build_review_checkpoint_application_proposal(
        execution, plan, boundary.to_revision, source,
    )
    assert isinstance(application.tail, ApplyCheckpointTailByFollowingBoundary)

    with pytest.raises(ValueError, match="following boundary source"):
        start_checkpoint_replay_tail(
            begin_checkpoint_stream_replay(application), boundary, None,
        )
    tail = start_checkpoint_replay_tail(
        begin_checkpoint_stream_replay(application), boundary, source,
    )
    with pytest.raises(ValueError, match="tail round disagrees"):
        advance_checkpoint_replay_tail_round(tail, outcomes[1])
    for outcome in outcomes:
        tail = advance_checkpoint_replay_tail_round(tail, outcome)
    assert finish_checkpoint_stream_replay(tail) == plan.checkpoint
    assert replay_review_checkpoint_application(
        application, outcomes, (), boundary, source,
    ) == plan.checkpoint
    with pytest.raises(ValueError, match="state differs"):
        finish_checkpoint_stream_replay(replace(
            tail, progress=replace(tail.progress, state=replace(tail.progress.state, failures=9)),
        ))


def test_retired_boundary_stream_replay_checks_before_state_and_late_outcome():
    initial = checkpoint(failures=1)
    first, late = failed(1), failed(2)
    before = project_review_execution(initial, (first,)).state
    after = carry_review_execution_generation(before, binding(1), 2)
    boundary = generation_boundary(before, after, 2, (1,), "boundary:stream-retired")
    trailing = failed(3, captured=binding(1))
    final = project_review_execution(ReviewExecutionCheckpoint(2, after), (trailing,)).state
    execution = ActivatedReviewExecution(
        ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin:stream-retired"),
        initial, final, 3, boundaries=(boundary,),
    )
    outcomes = (first, late, trailing)
    plan = plan_review_execution_compaction(execution, outcomes, 3)
    application = build_review_checkpoint_application_proposal(
        execution, plan, boundary.to_revision, None,
    )

    pending = start_checkpoint_replay_boundary(
        begin_checkpoint_stream_replay(application), boundary,
    )
    with pytest.raises(ValueError, match="boundary differs"):
        finish_checkpoint_replay_boundary(pending)
    pending = advance_checkpoint_replay_boundary_round(pending, first)
    pending = advance_checkpoint_replay_boundary_round(pending, late)
    progress = finish_checkpoint_replay_boundary(pending)
    tail = start_checkpoint_replay_tail(progress, None, None)
    tail = advance_checkpoint_replay_tail_round(tail, trailing)
    assert finish_checkpoint_stream_replay(tail) == plan.checkpoint
    assert replay_review_checkpoint_application(
        application, outcomes, (boundary,), None, None,
    ) == plan.checkpoint

    changed_before = replace(before, failures=9)
    wrong_before = replace(
        boundary, before=changed_before,
        after=carry_review_execution_generation(changed_before, binding(1), 2),
    )
    wrong = start_checkpoint_replay_boundary(
        begin_checkpoint_stream_replay(application), wrong_before,
    )
    wrong = advance_checkpoint_replay_boundary_round(wrong, first)
    wrong = advance_checkpoint_replay_boundary_round(wrong, late)
    with pytest.raises(ValueError, match="boundary differs"):
        finish_checkpoint_replay_boundary(wrong)


def test_first_retired_boundary_can_reference_a_compacted_prefix_outcome():
    initial = checkpoint()
    first, late = failed(1), failed(2)
    previous = project_review_execution(initial, (first,)).checkpoint
    after = carry_review_execution_generation(previous.state, binding(1), 2)
    boundary = generation_boundary(
        previous.state, after, 2, (1,), "boundary:stream-prefix",
    )
    target = ReviewExecutionCheckpoint(
        2, after, previous.policy,
        ReviewExecutionBoundaryCursor(boundary.receipt_id, boundary.to_revision),
    )
    application = ReviewCheckpointApplicationProposal(
        previous, target, boundary.to_revision, 2, ApplyAllCheckpointTailOutcomes(),
    )

    pending = start_checkpoint_replay_boundary(
        begin_checkpoint_stream_replay(application), boundary,
    )
    pending = advance_checkpoint_replay_boundary_round(pending, late)
    progress = finish_checkpoint_replay_boundary(pending)
    assert finish_checkpoint_stream_replay(
        start_checkpoint_replay_tail(progress, None, None),
    ) == target
    assert replay_review_checkpoint_application(
        application, (late,), (boundary,), None, None,
    ) == target


def test_all_tail_accepts_only_a_boundary_created_after_capture():
    initial = checkpoint()
    outcome = failed(1)
    target = project_review_execution(initial, (outcome,)).checkpoint
    application = ReviewCheckpointApplicationProposal(
        initial, target, WorkflowRevision(binding().rejection.fence, 0),
        0, ApplyAllCheckpointTailOutcomes(),
    )
    with pytest.raises(ValueError, match="generation frontier"):
        begin_checkpoint_stream_replay(replace(
            application,
            checkpoint=replace(target, through_sequence=MAX_EXECUTION_SUFFIX + 1),
        ))
    malformed = replace(application, tail_start_sequence=target.through_sequence)
    forged = _CheckpointReplayBetweenBoundaries(
        malformed, target.state, target.through_sequence + 1,
        initial.state.binding.rejection.fence.generation + 1, None,
    )
    with pytest.raises(ValueError, match="skipped its previous checkpoint"):
        start_checkpoint_replay_tail(forged, None, None)
    after = carry_review_execution_generation(target.state, binding(1), 1)
    later = generation_boundary(target.state, after, 1, (1,), "boundary:stream-later")
    source = ReviewGenerationArchiveFingerprint(
        ReviewExecutionBoundaryCursor(later.receipt_id, later.to_revision),
        "a" * 64, "workflow-request", "request:stream-later", "b" * 64,
    )

    tail = start_checkpoint_replay_tail(
        begin_checkpoint_stream_replay(application), later, source,
    )
    tail = advance_checkpoint_replay_tail_round(tail, outcome)
    assert finish_checkpoint_stream_replay(tail) == target
    with pytest.raises(ValueError, match="missed a captured following boundary"):
        start_checkpoint_replay_tail(
            begin_checkpoint_stream_replay(replace(
                application,
                captured_revision=WorkflowRevision(binding().rejection.fence,
                                                   later.to_revision.version),
            )), later, source,
        )


def test_zero_round_boundaries_replay_without_retaining_outcomes():
    initial = checkpoint()
    first_after = carry_review_execution_generation(initial.state, binding(1), 0)
    first = generation_boundary(initial.state, first_after, 0, (), "boundary:stream-zero:1")
    second_after = carry_review_execution_generation(first_after, binding(2), 0)
    second = generation_boundary(first_after, second_after, 0, (), "boundary:stream-zero:2")
    execution = ActivatedReviewExecution(
        ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin:stream-zero"),
        initial, second_after, 0, boundaries=(first, second),
    )
    plan = plan_review_execution_compaction(execution, (), 0)
    application = build_review_checkpoint_application_proposal(
        execution, plan, second.to_revision, None,
    )

    progress = begin_checkpoint_stream_replay(application)
    for boundary in (first, second):
        progress = finish_checkpoint_replay_boundary(
            start_checkpoint_replay_boundary(progress, boundary),
        )
    assert finish_checkpoint_stream_replay(
        start_checkpoint_replay_tail(progress, None, None),
    ) == plan.checkpoint
