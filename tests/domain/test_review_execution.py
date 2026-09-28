"""Ordered outer review outcomes and continuation contracts; no runtime cutover."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hashlib
from itertools import permutations

import pytest

from issue_agent.domain.agent_task import RUNTIME_CAPABILITIES, RuntimeName
from issue_agent.domain.review_execution import (
    MAX_EXECUTION_SUFFIX, MAX_ROUND_CHILDREN, ActivatedReviewExecution, AddReviewStrike, ClearReviewSession,
    CopiedReviewProgress, ImportedReviewExecutionDecision, ImportedReviewExecutionOriginReceipt,
    InitializedReviewExecutionOriginReceipt, initialized_review_execution,
    ImportedReviewSession, LaunchedReviewChild, NoReviewSession,
    PaidReviewProgress, PreserveDisplayedRound, PreserveReviewSession, ReplaceReviewSession,
    ReusableReviewSession, ReviewAdmissionRefused, ReviewCancellationReason, ReviewChildAdmissionRefusal, ReviewChildIdentity,
    ReviewChildKind, ReviewExecutionBinding, ReviewExecutionCheckpoint, ReviewExecutionPolicy,
    ReviewExecutionGenerationBoundary, ReviewExecutionOrigin, ReviewExecutionOriginKind, ReviewExecutionState,
    ReviewExecutionAcceptedTransitionSource, ReviewExecutionTransitionProofSource,
    accepted_review_transition_source, accepted_review_transition_proof_source,
    ReviewPinDeferred, ReviewPreparationDeferred, ReviewPreparationFailure,
    ReviewPreparationKind, ReviewReservationReleased, ReviewRoundCancelled, ReviewUnlaunchedRound,
    ReviewRoundFailure, ReviewRoundIdentity, ReviewRoundOutcome, SaturateReviewFailures,
    SetDisplayedRound, advance_review_execution_generation, carry_review_execution_generation,
    compatible_review_session, matches_accepted_review_transition_source,
    review_generation_chain_digest, review_generation_receipt_id, review_generation_within_frontier,
    plan_review_execution_compaction, project_review_execution, replay_review_execution_generations,
)
from issue_agent.domain.review_workflow import ReviewEpisode, ReviewFamilyEpisodes, ReviewWorkflowSnapshot
from issue_agent.domain.workflow import ObservedTransition, WorkflowMutation
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
)


MAX_INTEGER = 2**63 - 1


def test_imported_origin_contract_rejects_noninitial_or_malformed_activation_evidence():
    origin = ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin:1")
    initial = checkpoint()
    receipt = ImportedReviewExecutionOriginReceipt(
        origin, initial, "a" * 64, "b" * 64, "c" * 64, "d" * 64,
    )
    decision = ImportedReviewExecutionDecision(origin, initial, "a" * 64, "b" * 64)
    assert receipt.checkpoint == decision.checkpoint
    with pytest.raises(ValueError, match="initial checkpoint"):
        replace(receipt, checkpoint=checkpoint(through=1))
    with pytest.raises(ValueError, match="SHA-256"):
        replace(receipt, inventory_digest="x" * 64)
    with pytest.raises(ValueError, match="initial origin and checkpoint"):
        replace(decision, origin=ReviewExecutionOrigin(ReviewExecutionOriginKind.RENEWED, "origin:2"))
    with pytest.raises(ValueError, match="SHA-256"):
        replace(decision, source_digest="")


def test_generation_chain_digest_binds_order_and_rejects_malformed_receipts():
    assert review_generation_chain_digest(("boundary:1", "boundary:2")) != review_generation_chain_digest(
        ("boundary:2", "boundary:1"),
    )
    for malformed in ((), ("boundary:1", "boundary:1"), ([],), ("\ud800",),
                      tuple(f"boundary:{index}" for index in range(MAX_EXECUTION_SUFFIX + 1))):
        with pytest.raises(ValueError):
            review_generation_chain_digest(malformed)


def binding(generation=0):
    parent = WorkflowBinding(WorkflowFence(WorkflowKey("o/r", PullRequestRef(9)), generation),
                             FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1), "rejections:1")
    return ReviewExecutionBinding(parent, 2, "execution:1")


def test_initialized_origin_derives_fresh_state_and_requires_typed_activation_source():
    parent = binding().rejection
    activated_at = datetime(2026, 9, 28, tzinfo=timezone.utc)
    execution, receipt = initialized_review_execution(
        parent, 3, "pr-observation:one", "a" * 64, "b" * 64, "c" * 64, activated_at,
    )
    assert isinstance(receipt, InitializedReviewExecutionOriginReceipt)
    assert receipt.checkpoint == execution.checkpoint
    assert execution.state == ReviewExecutionState(execution.binding)
    with pytest.raises(ValueError, match="SHA-256"):
        replace(receipt, source_digest="invalid")
    with pytest.raises(ValueError, match="aware activation time"):
        replace(receipt, activated_at=activated_at.replace(tzinfo=None))
    with pytest.raises(ValueError, match="fresh sequence-zero"):
        replace(receipt, checkpoint=replace(receipt.checkpoint, state=replace(execution.state, failures=1)))


def round_id(sequence, *, captured=None):
    return ReviewRoundIdentity(captured or binding(), f"round:{sequence}", sequence)


def launched(round_, *, kind=ReviewChildKind.REVIEWER, child="reviewer"):
    identity = ReviewChildIdentity(round_, "review", child, kind)
    return LaunchedReviewChild(identity, f"run:{round_.round_id}:{child}", f"launch:{round_.round_id}:{child}")


def failed(sequence, *, captured=None, children=1, debit=AddReviewStrike()):
    round_ = round_id(sequence, captured=captured)
    paid = tuple(launched(round_, kind=ReviewChildKind.DIMENSION, child=f"dimension:{n}")
                 for n in range(children))
    return ReviewRoundOutcome(round_, ReviewRoundFailure(paid, f"failed:{sequence}", debit))


def productive(sequence, *, handle=None, clear=False, display=None, captured=None):
    round_ = round_id(sequence, captured=captured)
    reviewer = launched(round_)
    session = (ClearReviewSession(reviewer) if clear else
               ReplaceReviewSession(ReusableReviewSession(RuntimeName.CLAUDE, handle, reviewer))
               if handle else PreserveReviewSession())
    return ReviewRoundOutcome(round_, PaidReviewProgress(f"submitted:{sequence}", (reviewer,)), session,
                              SetDisplayedRound(display) if display is not None else PreserveDisplayedRound())


def copied(sequence, *, captured=None):
    return ReviewRoundOutcome(round_id(sequence, captured=captured), CopiedReviewProgress(f"copied:{sequence}"))


def pin(sequence, *, captured=None):
    return ReviewRoundOutcome(round_id(sequence, captured=captured), ReviewPinDeferred(f"preflight:{sequence}"))


def checkpoint(*, captured=None, through=0, policy=ReviewExecutionPolicy(), **state):
    return ReviewExecutionCheckpoint(through, ReviewExecutionState(captured or binding(), **state), policy)


def generation_boundary(before, after, through, applied, receipt):
    version = before.binding.rejection.fence.generation + 4
    return ReviewExecutionGenerationBoundary(
        before, after, through, applied, receipt,
        WorkflowRevision(before.binding.rejection.fence, version),
        WorkflowRevision(after.binding.rejection.fence, version + 1),
        WorkflowMutation.REQUEST_REARM,
    )


def test_child_refusal_requires_actual_cap_exhaustion_and_bound_revision():
    child = launched(round_id(1)).child
    now = datetime.now(timezone.utc)
    refusal = ReviewChildAdmissionRefusal(
        child, "receipt:1", "run:1", "reservation:1", "claim:1",
        WorkflowRevision(child.round.binding.rejection.fence, 1), "owner:1", "fingerprint:1",
        "o/r", "repository", "day:1", now, now + timedelta(days=1), "UTC", "USD",
        0, 60, 59, now,
    )
    assert refusal.committed_micros + refusal.requested_micros > refusal.cap_micros
    with pytest.raises(ValueError, match="cap exhaustion"):
        replace(refusal, cap_micros=60)
    with pytest.raises(ValueError, match="timestamps"):
        replace(refusal, refused_at=now.replace(tzinfo=None))
    with pytest.raises(ValueError, match="revision"):
        replace(refusal, refused_revision=WorkflowRevision(WorkflowFence(child.round.binding.rejection.fence.key, 1), 1))


def test_out_of_order_delivery_and_incremental_compaction_preserve_the_same_outcomes():
    initial = checkpoint(failures=3, pin_deferrals=2, policy=ReviewExecutionPolicy(free_pin_deferrals=1))
    outcomes = (failed(1), productive(2, handle="session:2", display=2),
                pin(3), failed(4, children=6), pin(5))
    expected = project_review_execution(initial, outcomes).state
    assert (expected.failures, expected.pin_deferrals, expected.displayed_round) == (2, 2, 2)
    assert expected.session.handle == "session:2"

    for delivery in permutations(outcomes):
        committed, pending, delivered = initial, {}, []
        for outcome in delivery:
            pending[outcome.round.sequence] = outcome
            delivered.append(outcome)
            projected = project_review_execution(committed, tuple(pending.values()))
            assert projected.state == project_review_execution(initial, tuple(delivered)).state
            committed = projected.checkpoint
            pending = {number: value for number, value in pending.items() if number > committed.through_sequence}
        assert committed.through_sequence == 5 and committed.state == expected
        assert pending == {}
    assert initial.state.failures == 3 and initial.through_sequence == 0


def test_generation_boundary_carries_prior_failures_and_ignores_late_old_rounds():
    initial = checkpoint(failures=2, pin_deferrals=1)
    prior = project_review_execution(initial, (failed(1),)).state
    carried = carry_review_execution_generation(prior, binding(1), 2)
    boundary = generation_boundary(prior, carried, 2, (1,), "boundary:1")

    observed = replay_review_execution_generations(
        initial, (failed(3, captured=binding(1)), failed(2), failed(1)), (boundary,),
    )

    assert observed.binding == binding(1)
    assert (observed.failures, observed.pin_deferrals) == (4, 1)
    assert observed.session_sequence == 2


def test_generation_replay_from_verified_prefix_inside_a_boundary_matches_full_history():
    initial = checkpoint(failures=2)
    first = failed(1)
    before = project_review_execution(initial, (first,)).state
    after = carry_review_execution_generation(before, binding(1), 2)
    boundary = generation_boundary(before, after, 2, (1,), "boundary:1")
    late = failed(2)
    next_round = failed(3, captured=binding(1))
    full = replay_review_execution_generations(initial, (first, late, next_round), (boundary,))
    compacted = project_review_execution(initial, (first,)).checkpoint

    assert replay_review_execution_generations(compacted, (late, next_round), (boundary,)) == full


def test_repeated_generation_boundaries_preserve_only_pre_advance_effects():
    initial = checkpoint(failures=1)
    first_before = project_review_execution(initial, (failed(1),)).state
    first_after = carry_review_execution_generation(first_before, binding(1), 2)
    first = generation_boundary(first_before, first_after, 2, (1,), "boundary:1")
    second_before = project_review_execution(
        ReviewExecutionCheckpoint(2, first_after), (failed(3, captured=binding(1)),),
    ).state
    second_after = carry_review_execution_generation(second_before, binding(2), 3)
    second = generation_boundary(second_before, second_after, 3, (3,), "boundary:2")

    observed = replay_review_execution_generations(
        initial, (failed(2), failed(1), failed(3, captured=binding(1)),
                  copied(4, captured=binding(2))), (first, second),
    )

    assert observed.binding == binding(2)
    assert observed.failures == 0 and observed.productive_sequence == 4
    backwards = replace(
        second, from_revision=WorkflowRevision(binding(1).rejection.fence, 1),
        to_revision=WorkflowRevision(binding(2).rejection.fence, 2),
    )
    with pytest.raises(ValueError, match="advance one execution history"):
        replay_review_execution_generations(
            initial, (failed(1), failed(3, captured=binding(1))), (first, backwards),
        )


def test_generation_boundary_clears_verified_reviewer_session_for_fresh_review():
    initial = checkpoint()
    prior = project_review_execution(initial, (productive(1, handle="session:1", display=7),)).state

    carried = carry_review_execution_generation(prior, binding(1), 1)

    assert isinstance(carried.session, NoReviewSession)
    assert carried.session_sequence == 1
    assert carried.displayed_round == 7 and carried.display_sequence == 1
    assert carried.productive_sequence == 1
    assert replay_review_execution_generations(
        initial, (productive(1, handle="session:1", display=7),),
        (generation_boundary(prior, carried, 1, (1,), "boundary:1"),),
    ) == carried


def test_generation_boundary_refuses_missing_source_and_changed_carry():
    initial = checkpoint()
    before = project_review_execution(initial, (failed(1),)).state
    after = carry_review_execution_generation(before, binding(1), 1)
    boundary = generation_boundary(before, after, 1, (1,), "boundary:1")

    with pytest.raises(ValueError, match="finalized outcome"):
        replay_review_execution_generations(initial, (), (boundary,))
    with pytest.raises(ValueError, match="prior outcome projection"):
        replay_review_execution_generations(initial, (copied(1),), (boundary,))
    with pytest.raises(ValueError, match="round allocation"):
        replay_review_execution_generations(initial, (failed(1, captured=binding(1)),), (boundary,))
    with pytest.raises(ValueError, match="carry the prior allowance"):
        replace(boundary, after=replace(after, failures=0))
    with pytest.raises(ValueError, match="ordered and allocated"):
        replace(boundary, applied_sequences=(1, 1))
    with pytest.raises(ValueError, match="next rejection generation"):
        carry_review_execution_generation(before, binding(2), 1)
    with pytest.raises(ValueError, match="exact advancing transition"):
        replace(boundary, to_revision=WorkflowRevision(binding(1).rejection.fence, 100))
    with pytest.raises(ValueError, match="exact advancing transition"):
        replace(boundary, mutation="request_rearm")


def test_generation_transition_source_binds_payload_without_retaining_it():
    initial = checkpoint()
    carried = carry_review_execution_generation(initial.state, binding(1), 0)
    boundary = generation_boundary(initial.state, carried, 0, (), "boundary:source")
    accepted = "accepted-transition:" + "x" * 100_000
    source = accepted_review_transition_source(boundary, "workflow-request", "request:1", accepted)

    assert isinstance(source, ReviewExecutionAcceptedTransitionSource)
    assert matches_accepted_review_transition_source(
        source, boundary, "workflow-request", "request:1", accepted,
    )
    assert not matches_accepted_review_transition_source(
        source, boundary, "workflow-request", "request:1", accepted + "changed",
    )
    assert matches_accepted_review_transition_source(
        (boundary, "workflow-request", "request:1", accepted),
        boundary, "workflow-request", "request:1", accepted,
    )
    assert len(source.payload_sha256) == 64 and accepted not in repr(source)
    with pytest.raises(ValueError, match="SHA-256"):
        replace(source, payload_sha256="not-a-digest")


def test_generation_transition_proof_binds_a_typed_release_cause():
    initial = checkpoint()
    carried = carry_review_execution_generation(initial.state, binding(1), 0)
    boundary = replace(
        generation_boundary(initial.state, carried, 0, (), "boundary:release"),
        mutation=WorkflowMutation.RELEASE_CEILING,
    )
    accepted = "accepted-release:" + "x" * 100_000
    cause = ObservedTransition(("release:evidence",))
    proof = accepted_review_transition_proof_source(
        boundary, "workflow-transition", "transition:key", accepted, cause, "encoded-release-audit",
    )

    assert isinstance(proof, ReviewExecutionTransitionProofSource)
    assert proof.audit_sha256 == hashlib.sha256(b"encoded-release-audit").hexdigest()
    assert accepted not in repr(proof)
    assert matches_accepted_review_transition_source(
        proof, boundary, "workflow-transition", "transition:key", accepted,
    )
    assert not matches_accepted_review_transition_source(
        proof, boundary, "workflow-transition", "transition:key", accepted + "changed",
    )
    with pytest.raises(ValueError, match="release proof"):
        replace(proof, namespace="workflow-request")
    with pytest.raises(ValueError, match="SHA-256 audit"):
        replace(proof, audit_sha256="untyped")
    with pytest.raises(ValueError, match="release proof"):
        accepted_review_transition_proof_source(
            boundary, "workflow-transition", "transition:key", accepted, "untyped", "encoded-release-audit",
        )


def test_generation_receipt_identity_is_reconstructible_from_episode_and_destination():
    component = activated(origin=ReviewExecutionOriginKind.IMPORTED)
    from_revision = WorkflowRevision(component.binding.rejection.fence, 7)
    to_revision = WorkflowRevision(binding(1).rejection.fence, 8)
    advanced = advance_review_execution_generation(
        component, from_revision, to_revision, WorkflowMutation.REQUEST_REARM, (),
    )
    episode = component.binding

    assert advanced.boundaries[0].receipt_id == review_generation_receipt_id(episode, 1)
    assert review_generation_receipt_id(episode, 1) == review_generation_receipt_id(
        advanced.binding, 1,
    )
    assert review_generation_receipt_id(episode, 1) != review_generation_receipt_id(episode, 2)
    with pytest.raises(ValueError, match="generation"):
        review_generation_receipt_id(episode, 0)


def test_generation_frontier_classifies_swept_episode_receipts():
    initial = checkpoint()
    origin = initial.state.binding
    after = carry_review_execution_generation(initial.state, binding(1), 0)
    boundary = generation_boundary(initial.state, after, 0, (), "boundary:frontier")

    assert review_generation_within_frontier(origin, 1, boundary)
    assert not review_generation_within_frontier(origin, 0, boundary)
    assert not review_generation_within_frontier(binding(1), 1, boundary)
    assert not review_generation_within_frontier(
        replace(origin, execution_episode="other-episode"), 1, boundary,
    )
    with pytest.raises(ValueError, match="generation frontier"):
        review_generation_within_frontier(origin, -1, boundary)
    with pytest.raises(ValueError, match="typed episode"):
        review_generation_within_frontier(origin, 1, "untyped")


def test_active_generation_advance_preserves_imported_allowance_without_rounds():
    component = activated(origin=ReviewExecutionOriginKind.IMPORTED, failures=3, pin_deferrals=2)
    from_revision = WorkflowRevision(component.binding.rejection.fence, 7)
    to_revision = WorkflowRevision(binding(1).rejection.fence, 8)

    advanced = advance_review_execution_generation(
        component, from_revision, to_revision, WorkflowMutation.REQUEST_REARM, (),
    )

    assert advanced.binding == binding(1)
    assert (advanced.state.failures, advanced.state.pin_deferrals) == (3, 2)
    assert advanced.checkpoint == component.checkpoint and advanced.last_sequence == 0
    assert replay_review_execution_generations(
        advanced.checkpoint, (), advanced.boundaries,
    ) == advanced.state


def test_compaction_after_late_old_round_preserves_carried_state_and_new_suffix():
    initial = checkpoint(failures=2)
    first = failed(1)
    productive_old = copied(2)
    before = project_review_execution(initial, (productive_old,)).state
    after = carry_review_execution_generation(before, binding(1), 2)
    boundary = generation_boundary(before, after, 2, (2,), "boundary:late")
    new_failure = failed(3, captured=binding(1))
    outcomes = (first, productive_old, new_failure)
    current_state = replay_review_execution_generations(initial, outcomes, (boundary,))
    component = ActivatedReviewExecution(
        ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin:late"),
        initial, current_state, 3, boundaries=(boundary,),
    )

    with pytest.raises(ValueError, match="open or missing"):
        plan_review_execution_compaction(component, (productive_old, new_failure), 2)
    plan = plan_review_execution_compaction(component, outcomes, 2)

    assert plan.checkpoint.through_sequence == 2
    assert plan.checkpoint.state == after
    assert plan.last_boundary_receipt == "boundary:late"
    assert plan.retired_outcomes == (first, productive_old)
    assert plan.remaining_outcomes == (new_failure,)
    assert project_review_execution(plan.checkpoint, plan.remaining_outcomes).state == current_state
    assert plan_review_execution_compaction(component, tuple(reversed(outcomes)), 2) == plan


def test_zero_round_advances_compact_at_same_sequence_with_distinct_boundary_frontiers():
    initial = checkpoint()
    first_after = carry_review_execution_generation(initial.state, binding(1), 0)
    first = generation_boundary(initial.state, first_after, 0, (), "boundary:zero:1")
    second_after = carry_review_execution_generation(first_after, binding(2), 0)
    second = generation_boundary(first_after, second_after, 0, (), "boundary:zero:2")
    component = ActivatedReviewExecution(
        ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin:zero"),
        initial, second_after, 0, boundaries=(first, second),
    )

    plan = plan_review_execution_compaction(component, (), 0)

    assert plan.checkpoint.state == second_after
    assert plan.last_boundary_receipt == "boundary:zero:2"
    assert plan.last_boundary_revision == second.to_revision
    assert plan.retired_boundaries == (first, second)
    assert plan.remaining_outcomes == ()
    next_outcome = failed(1, captured=binding(2))
    next_state = project_review_execution(plan.checkpoint, (next_outcome,)).state
    later = ActivatedReviewExecution(component.origin, plan.checkpoint, next_state, 1)
    later_plan = plan_review_execution_compaction(later, (next_outcome,), 1)
    assert later_plan.retired_boundaries == ()
    assert later_plan.last_boundary_receipt == "boundary:zero:2"
    assert later_plan.last_boundary_revision == second.to_revision
    with pytest.raises(ValueError, match="new finalized frontier"):
        plan_review_execution_compaction(activated(), (), 0)


def test_partial_compaction_keeps_a_later_boundary_and_ignores_late_old_outcome():
    initial = checkpoint()
    settled = (failed(1), failed(2))
    before = project_review_execution(initial, (failed(2),)).state
    after = carry_review_execution_generation(before, binding(1), 3)
    boundary = generation_boundary(before, after, 3, (2,), "boundary:open-third")
    component = ActivatedReviewExecution(
        ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin:partial"),
        initial, after, 3, boundaries=(boundary,),
    )

    plan = plan_review_execution_compaction(component, settled, 2)

    assert plan.checkpoint.through_sequence == 2
    assert plan.checkpoint.state == before
    assert plan.retired_boundaries == ()
    assert plan.retired_outcomes == settled
    assert plan.remaining_boundaries == (boundary,)
    assert replay_review_execution_generations(plan.checkpoint, (failed(3),),
                                               plan.remaining_boundaries) == after


@pytest.mark.parametrize("outcomes", [(None,), (failed(1), failed(1)), (failed(2),)])
def test_compaction_refuses_invalid_or_noncontiguous_source_sets(outcomes):
    component = replace(activated(), state=replace(activated().state, failures=1), last_sequence=2)
    with pytest.raises(ValueError, match="invalid outcome|open or missing|exact allocated"):
        plan_review_execution_compaction(component, outcomes, 2)


def test_compaction_refuses_future_or_duplicated_state_neutral_outcomes():
    neutral = ReviewRoundOutcome(
        round_id(1), ReviewRoundCancelled(ReviewCancellationReason.SHUTDOWN, "shutdown:1"),
    )
    future = ReviewRoundOutcome(
        round_id(2), ReviewRoundCancelled(ReviewCancellationReason.SHUTDOWN, "shutdown:2"),
    )
    component = replace(activated(), last_sequence=1)

    with pytest.raises(ValueError, match="exact allocated"):
        plan_review_execution_compaction(component, (neutral, future), 1)
    with pytest.raises(ValueError, match="exact allocated"):
        plan_review_execution_compaction(component, (neutral, neutral), 1)


def test_a_gap_is_not_compacted_and_filling_it_does_not_charge_later_rounds_twice():
    start = checkpoint(failures=4)
    later = project_review_execution(start, (failed(3),))
    assert later.state.failures == 5 and later.latest_sequence == 3
    assert later.checkpoint == start
    partial = project_review_execution(later.checkpoint, (failed(3), copied(1)))
    assert partial.state.failures == 1
    assert partial.checkpoint.through_sequence == 1 and partial.checkpoint.state.failures == 0
    complete = project_review_execution(partial.checkpoint, (failed(3), pin(2)))
    assert (complete.state.failures, complete.state.pin_deferrals) == (1, 1)
    assert complete.checkpoint.through_sequence == 3
    assert project_review_execution(complete.checkpoint, ()).state == complete.state


def test_compacted_pin_history_keeps_its_captured_policy_for_later_outcomes():
    start = checkpoint(policy=ReviewExecutionPolicy(free_pin_deferrals=1))
    prefix = project_review_execution(start, (pin(1),)).checkpoint
    resumed = project_review_execution(prefix, (pin(2), pin(3)))
    replayed = project_review_execution(start, (pin(1), pin(2), pin(3)))
    assert resumed == replayed
    assert resumed.state.failures == 2


def test_exact_suffix_replay_is_idempotent_but_compacted_replay_requires_receipt_verification():
    outcome = failed(1)
    once = project_review_execution(checkpoint(), (outcome,))
    assert project_review_execution(checkpoint(), (outcome, outcome)) == once
    with pytest.raises(ValueError, match="compacted.*receipt"):
        project_review_execution(once.checkpoint, (outcome,))


@pytest.mark.parametrize("conflict", ["disposition", "receipt", "round_id", "sequence"])
def test_conflicting_round_replay_cannot_buy_or_erase_an_allowance(conflict):
    original = failed(1)
    if conflict == "disposition":
        changed = copied(1)
    elif conflict == "receipt":
        assert isinstance(original.disposition, ReviewRoundFailure)
        changed = replace(original, disposition=replace(original.disposition, outcome_receipt="different"))
    elif conflict == "round_id":
        changed = ReviewRoundOutcome(replace(original.round, round_id="another-round"), CopiedReviewProgress("receipt"))
    else:
        changed = ReviewRoundOutcome(replace(original.round, sequence=2), CopiedReviewProgress("receipt"))
    with pytest.raises(ValueError, match="conflicting|multiple identities"):
        project_review_execution(checkpoint(), (original, changed))


def test_progress_only_clears_failures_and_pin_deferrals_from_earlier_rounds():
    projection = project_review_execution(checkpoint(failures=7, pin_deferrals=8, policy=ReviewExecutionPolicy(0)),
                                         (pin(4), failed(3), copied(2), pin(1)))
    assert (projection.state.failures, projection.state.pin_deferrals) == (2, 1)
    assert projection.state.productive_sequence == 2


def test_persistent_pin_failures_use_the_free_bound_and_reset_after_verified_progress():
    first = project_review_execution(checkpoint(), tuple(pin(n) for n in range(1, 8)))
    assert (first.state.failures, first.state.pin_deferrals) == (2, 7)
    reset = project_review_execution(first.checkpoint, (copied(8), pin(9), pin(10)))
    assert (reset.state.failures, reset.state.pin_deferrals) == (0, 2)
    assert reset.state.productive_sequence == 8


@pytest.mark.parametrize("children", [1, 6, MAX_ROUND_CHILDREN])
def test_a_failed_multi_review_charges_one_outer_round(children):
    result = project_review_execution(checkpoint(failures=2), (failed(1, children=children),))
    assert result.state.failures == 3


def test_deterministic_death_saturates_once_preserves_larger_count_and_progress_clears_it():
    saturated = failed(1, children=6, debit=SaturateReviewFailures(5, "deterministic:input"))
    assert project_review_execution(checkpoint(failures=1), (saturated,)).state.failures == 5
    assert project_review_execution(checkpoint(failures=8), (saturated,)).state.failures == 8
    result = project_review_execution(checkpoint(), (failed(3), copied(2), saturated))
    assert result.state.failures == 1


def test_counters_saturate_at_the_integer_bound_instead_of_wrapping_or_refusing_progress():
    result = project_review_execution(checkpoint(failures=MAX_INTEGER, pin_deferrals=MAX_INTEGER,
                                              policy=ReviewExecutionPolicy(0)), (failed(1), pin(2)))
    assert result.state.failures == result.state.pin_deferrals == MAX_INTEGER
    assert project_review_execution(result.checkpoint, (copied(3),)).state.failures == 0


@pytest.mark.parametrize("disposition", [
    ReviewAdmissionRefused("budget-refusal"), ReviewReservationReleased("reservation", "never-launched"),
    ReviewUnlaunchedRound("complete-closure"),
    ReviewPreparationDeferred(ReviewPreparationFailure(ReviewPreparationKind.RUNTIME, "preflight-failed")),
    *(ReviewRoundCancelled(reason, f"cancel:{reason.value}") for reason in ReviewCancellationReason),
])
def test_refusal_never_launched_release_and_operator_cancellation_preserve_the_allowance(disposition):
    session = ReusableReviewSession(RuntimeName.CLAUDE, "retained", ImportedReviewSession("legacy:verified"))
    before = checkpoint(failures=2, pin_deferrals=3, displayed_round=7, session=session)
    result = project_review_execution(before, (ReviewRoundOutcome(round_id(1), disposition),))
    assert result.state == before.state
    assert result.checkpoint.through_sequence == 1


def test_newer_sessionless_reviewer_clears_older_session_regardless_of_return_order():
    outcomes = (productive(1, handle="older", display=1), productive(2, clear=True, display=2), copied(3))
    for delivery in permutations(outcomes):
        result = project_review_execution(checkpoint(), delivery).state
        assert isinstance(result.session, NoReviewSession)
        assert result.session_sequence == 2 and result.displayed_round == 2
        assert result.display_sequence == 2 and result.productive_sequence == 3


def test_later_eligible_reviewer_replaces_a_cleared_session():
    result = project_review_execution(checkpoint(), (productive(2, handle="newer"), productive(1, clear=True)))
    assert result.state.session.handle == "newer" and result.state.session_sequence == 2


def test_gate_and_free_copied_progress_preserve_session_and_displayed_round():
    original = productive(1, handle="reviewer-session", display=4)
    gate_round = round_id(2)
    gate = launched(gate_round, kind=ReviewChildKind.GATE, child="gate")
    gate_progress = ReviewRoundOutcome(gate_round, PaidReviewProgress("gate-approved", (gate,)))
    result = project_review_execution(checkpoint(), (copied(4), failed(3), gate_progress, original)).state
    assert result.failures == 0 and result.productive_sequence == 4
    assert result.session.handle == "reviewer-session" and result.session_sequence == 1
    assert (result.displayed_round, result.display_sequence) == (4, 1)


@pytest.mark.parametrize("runtime", list(RuntimeName))
@pytest.mark.parametrize("supports_resume,preserves_sandbox", [(True, True), (True, False), (False, True), (False, False)])
def test_session_reuse_requires_same_runtime_and_sandbox_preserving_resume(runtime, supports_resume, preserves_sandbox):
    session = ReusableReviewSession(RuntimeName.CLAUDE, "session", ImportedReviewSession("legacy:verified"))
    capabilities = replace(RUNTIME_CAPABILITIES[runtime], supports_resume=supports_resume,
                           resume_preserves_sandbox=preserves_sandbox)
    selected = compatible_review_session(session, runtime, capabilities)
    if runtime is RuntimeName.CLAUDE and supports_resume and preserves_sandbox:
        assert selected == session
    else:
        assert isinstance(selected, NoReviewSession)
    assert isinstance(compatible_review_session(NoReviewSession(), runtime, capabilities), NoReviewSession)


@pytest.mark.parametrize("outcome", [failed(1), productive(1, handle="stale", display=9), pin(1), copied(1)])
def test_old_generation_outcomes_are_auditable_without_changing_new_generation_state(outcome):
    current = checkpoint(captured=binding(1), failures=3, pin_deferrals=2, displayed_round=4)
    result = project_review_execution(current, (outcome,))
    assert result.state == current.state
    assert result.checkpoint.through_sequence == result.latest_sequence == 1


@pytest.mark.parametrize("change", ["execution_epoch", "execution_episode", "rejection_epoch", "rejection_episode",
                                    "repository", "pr", "future_generation"])
def test_another_authority_episode_or_future_capture_cannot_mutate_the_projection(change):
    captured = binding()
    if change == "execution_epoch":
        captured = replace(captured, execution_epoch=3)
    elif change == "execution_episode":
        captured = replace(captured, execution_episode="execution:other")
    else:
        parent = captured.rejection
        if change == "rejection_epoch":
            parent = replace(parent, authority=FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 2))
        elif change == "rejection_episode":
            parent = replace(parent, episode_id="rejections:other")
        elif change == "future_generation":
            parent = replace(parent, fence=replace(parent.fence, generation=1))
        else:
            key = WorkflowKey("other/repo", PullRequestRef(9)) if change == "repository" else WorkflowKey("o/r", PullRequestRef(10))
            parent = replace(parent, fence=replace(parent.fence, key=key))
        captured = replace(captured, rejection=parent)
    with pytest.raises(ValueError, match="authority|episode|future generation"):
        project_review_execution(checkpoint(), (copied(1, captured=captured),))


def test_retained_suffix_is_bounded_without_silently_discarding_an_unresolved_gap():
    outcomes = tuple(pin(sequence) for sequence in range(2, MAX_EXECUTION_SUFFIX + 2))
    result = project_review_execution(checkpoint(), outcomes)
    assert result.checkpoint.through_sequence == 0
    assert result.state.pin_deferrals == MAX_EXECUTION_SUFFIX
    with pytest.raises(ValueError, match="bounded tuple"):
        project_review_execution(checkpoint(), outcomes + (pin(MAX_EXECUTION_SUFFIX + 2),))


@pytest.mark.parametrize("bad", [None, True, 0, -1, 2**63, "1"])
def test_positive_execution_identities_reject_malformed_and_oversized_numbers(bad):
    for construct in (lambda: ReviewExecutionBinding(binding().rejection, bad, "episode"),
                      lambda: ReviewRoundIdentity(binding(), "round", bad),
                      lambda: SetDisplayedRound(bad),
                      lambda: SaturateReviewFailures(bad, "receipt")):
        with pytest.raises(ValueError):
            construct()


@pytest.mark.parametrize("bad", [None, True, -1, 2**63, "0"])
def test_counter_and_watermark_fields_require_bounded_integers(bad):
    for field in ("failures", "pin_deferrals", "session_sequence", "displayed_round", "display_sequence", "productive_sequence"):
        with pytest.raises(ValueError):
            ReviewExecutionState(binding(), **{field: bad})
    with pytest.raises(ValueError):
        ReviewExecutionCheckpoint(bad, ReviewExecutionState(binding()))
    with pytest.raises(ValueError):
        ReviewExecutionPolicy(bad)


@pytest.mark.parametrize("bad", [None, "", " ", "bad\x00id", "line\nbreak", "x" * 513])
def test_external_identifiers_and_receipt_references_are_validated(bad):
    for construct in (lambda: ReviewRoundIdentity(binding(), bad, 1),
                      lambda: ReviewExecutionBinding(binding().rejection, 1, bad),
                      lambda: ImportedReviewSession(bad), lambda: CopiedReviewProgress(bad),
                      lambda: ReviewPinDeferred(bad), lambda: ReviewAdmissionRefused(bad),
                      lambda: ReviewReservationReleased("reservation", bad)):
        with pytest.raises(ValueError):
            construct()


@pytest.mark.parametrize("marker", ["multi", "github-review", "codex-review"])
def test_legacy_mode_markers_never_become_reusable_sessions(marker):
    with pytest.raises(ValueError, match="mode markers"):
        ReusableReviewSession(RuntimeName.CLAUDE, marker, ImportedReviewSession("legacy:verified"))


@pytest.mark.parametrize("kind", [ReviewChildKind.GATE, ReviewChildKind.DIMENSION, ReviewChildKind.VALIDATOR])
def test_auxiliary_child_cannot_replace_or_clear_the_reviewer_session(kind):
    child = launched(round_id(1), kind=kind)
    with pytest.raises(ValueError, match="reviewer"):
        ReusableReviewSession(RuntimeName.CLAUDE, "auxiliary-handle", child)
    with pytest.raises(ValueError, match="reviewer"):
        ClearReviewSession(child)


def test_session_update_requires_this_rounds_exact_launched_producer():
    outcome = productive(1)
    another = launched(round_id(2))
    with pytest.raises(ValueError, match="this outcome"):
        replace(outcome, session=ClearReviewSession(another))
    same_round_other_run = replace(launched(outcome.round), run_id="unlisted-run")
    with pytest.raises(ValueError, match="this outcome"):
        replace(outcome, session=ClearReviewSession(same_round_other_run))
    imported = ReusableReviewSession(RuntimeName.CLAUDE, "imported", ImportedReviewSession("legacy:verified"))
    with pytest.raises(ValueError, match="launched reviewer"):
        ReplaceReviewSession(imported)


@pytest.mark.parametrize("outcome", [copied(1), pin(1), ReviewRoundOutcome(round_id(1), ReviewAdmissionRefused("refusal"))])
def test_copy_pin_and_refusal_preserve_reviewer_session_and_display_number(outcome):
    with pytest.raises(ValueError, match="productive reviewer"):
        replace(outcome, session=ClearReviewSession(launched(outcome.round)))
    with pytest.raises(ValueError, match="only paid productive"):
        replace(outcome, display=SetDisplayedRound(3))


def test_failed_paid_reviewer_preserves_the_last_productive_session():
    prior = productive(1, handle="last-confirmed", display=1)
    round_ = round_id(2)
    child = launched(round_)
    failure = ReviewRoundOutcome(round_, ReviewRoundFailure((child,), "refused-submission"))
    for session in (ClearReviewSession(child),
                    ReplaceReviewSession(ReusableReviewSession(RuntimeName.CLAUDE, "unconfirmed", child))):
        with pytest.raises(ValueError, match="productive reviewer"):
            replace(failure, session=session)
    result = project_review_execution(checkpoint(), (failure, prior)).state
    assert result.session.handle == "last-confirmed" and result.session_sequence == 1
    assert result.displayed_round == 1 and result.failures == 1


@pytest.mark.parametrize("bad", ["empty", "list", "untyped", "duplicate_run", "duplicate_child", "different_round", "huge"])
def test_paid_outcome_requires_bounded_distinct_launches_from_one_outer_round(bad):
    first = launched(round_id(1))
    launches = {
        "empty": (), "list": [first], "untyped": (None,), "duplicate_run": (first, first),
        "duplicate_child": (first, replace(first, run_id="other-run")),
        "different_round": (first, launched(round_id(2), child="other")),
        "huge": tuple(launched(round_id(1), child=f"child:{n}") for n in range(MAX_ROUND_CHILDREN + 1)),
    }[bad]
    for construct in (lambda: PaidReviewProgress("receipt", launches),
                      lambda: ReviewRoundFailure(launches, "receipt")):
        with pytest.raises(ValueError):
            construct()


def test_outcome_and_projection_validate_launch_and_session_capture_against_their_round():
    with pytest.raises(ValueError, match="another round"):
        ReviewRoundOutcome(round_id(2), productive(1).disposition)
    session = productive(2, handle="session:2").session.session
    with pytest.raises(ValueError, match="sequence"):
        ReviewExecutionState(binding(), session=session, session_sequence=1)
    with pytest.raises(ValueError, match="binding"):
        ReviewExecutionState(binding(1), session=session, session_sequence=2)
    with pytest.raises(ValueError, match="future outcome"):
        checkpoint(through=1, session=session, session_sequence=2)
    imported = ReusableReviewSession(RuntimeName.CLAUDE, "legacy", ImportedReviewSession("legacy:verified"))
    with pytest.raises(ValueError, match="precedes"):
        ReviewExecutionState(binding(), session=imported, session_sequence=1)


def test_execution_cannot_be_attached_to_resolve_or_untyped_parent_authority():
    resolve = WorkflowBinding(WorkflowFence(WorkflowKey("o/r", IssueRef(9)), 0),
                              FamilyAuthority(WorkflowFamily.RESOLVE, 1), "resolve:1")
    for parent in (resolve, None, "review"):
        with pytest.raises(ValueError, match="parent rejection"):
            ReviewExecutionBinding(parent, 1, "execution:1")


@pytest.mark.parametrize("bad", [None, [], (None,), ("outcome",)])
def test_projection_rejects_untyped_or_mutable_suffixes(bad):
    with pytest.raises(ValueError):
        project_review_execution(checkpoint(), bad)


def test_execution_values_are_immutable_and_empty_projection_preserves_initial_authority():
    initial = checkpoint(failures=4)
    result = project_review_execution(initial, ())
    assert result.state == initial.state and result.checkpoint == initial and result.latest_sequence == 0
    with pytest.raises(FrozenInstanceError):
        setattr(initial.state, "failures", 0)


def activated(*, captured=None, origin=ReviewExecutionOriginKind.INITIALIZED, **state_fields):
    initial = checkpoint(captured=captured, **state_fields)
    return ActivatedReviewExecution(ReviewExecutionOrigin(origin, "origin:verified"), initial, initial.state, 0)


def composed_review(component=None):
    parent = binding().rejection
    return ReviewWorkflowSnapshot(WorkflowRevision(parent.fence, 4), parent.authority,
                                  ReviewEpisode(parent.episode_id, "preserve rejection guidance"),
                                  execution=component or activated())


def test_composed_execution_has_its_own_episode_without_replacing_rejection_authority():
    state = composed_review()
    assert state.binding == binding().rejection
    assert state.execution.binding.execution_epoch == 2
    assert state.execution.binding.execution_episode == "execution:1"
    assert state.episode.episode_id == "rejections:1"
    assert state.episode.guidance == "preserve rejection guidance"
    envelope = ReviewFamilyEpisodes(state.authority, state.episode, state.execution)
    assert envelope.execution == state.execution
    with pytest.raises(FrozenInstanceError):
        setattr(envelope, "execution", None)


@pytest.mark.parametrize("mismatch", ["repository", "target", "generation", "rejection_epoch", "rejection_episode"])
def test_execution_component_cannot_attach_to_another_parent_binding(mismatch):
    parent = binding().rejection
    if mismatch in {"repository", "target", "generation"}:
        key = parent.fence.key
        if mismatch == "repository":
            key = WorkflowKey("other/repo", key.target)
        elif mismatch == "target":
            key = WorkflowKey(key.repository, PullRequestRef(99))
        parent = replace(parent, fence=replace(parent.fence, key=key, generation=1 if mismatch == "generation" else 0))
    elif mismatch == "rejection_epoch":
        parent = replace(parent, authority=replace(parent.authority, epoch=99))
    else:
        parent = replace(parent, episode_id="another-rejection-episode")
    component = activated(captured=replace(binding(), rejection=parent))
    with pytest.raises(ValueError, match="review workflow binding"):
        composed_review(component)
    if mismatch in {"rejection_epoch", "rejection_episode"}:
        state = composed_review()
        with pytest.raises(ValueError, match="rejection authority"):
            ReviewFamilyEpisodes(state.authority, state.episode, component)


@pytest.mark.parametrize("mismatch", ["execution_epoch", "execution_episode"])
def test_checkpoint_and_current_execution_cannot_mix_distinct_episodes(mismatch):
    component = activated()
    changed = replace(component.binding, **{mismatch: 3 if mismatch == "execution_epoch" else "another-execution"})
    with pytest.raises(ValueError, match="different bindings"):
        replace(component, state=replace(component.state, binding=changed))


@pytest.mark.parametrize("field", ["session_sequence", "display_sequence", "productive_sequence"])
def test_active_execution_rejects_a_current_watermark_beyond_its_last_allocated_round(field):
    component = activated()
    with pytest.raises(ValueError, match="future outcome"):
        replace(component, state=replace(component.state, **{field: 2}), last_sequence=1)


@pytest.mark.parametrize("invalid", [None, True, -1, 1.5, "1", 2**63])
def test_active_execution_last_sequence_rejects_invalid_or_overflow_values(invalid):
    with pytest.raises(ValueError):
        replace(activated(), last_sequence=invalid)


def test_checkpoint_cannot_cover_unallocated_rounds_or_disagree_with_its_complete_projection():
    component = activated()
    with pytest.raises(ValueError, match="exceeds allocated"):
        replace(component, checkpoint=replace(component.checkpoint, through_sequence=1))
    completed = project_review_execution(component.checkpoint, (failed(1),)).checkpoint
    with pytest.raises(ValueError, match="completed checkpoint"):
        replace(component, checkpoint=completed, state=replace(completed.state, failures=99), last_sequence=1)


@pytest.mark.parametrize("origin", [ReviewExecutionOriginKind.INITIALIZED, ReviewExecutionOriginKind.RENEWED])
def test_imported_session_can_only_enter_an_imported_execution_origin(origin):
    session = ReusableReviewSession(RuntimeName.CLAUDE, "legacy:session", ImportedReviewSession("import:session"))
    accepted = activated(origin=ReviewExecutionOriginKind.IMPORTED, failures=4, pin_deferrals=7, session=session)
    assert accepted.state.failures == 4 and accepted.state.pin_deferrals == 7 and accepted.state.session == session
    with pytest.raises(ValueError, match="imported execution origin"):
        replace(accepted, origin=ReviewExecutionOrigin(origin, "new:origin"))


@pytest.mark.parametrize("origin", [ReviewExecutionOriginKind.INITIALIZED, ReviewExecutionOriginKind.RENEWED])
@pytest.mark.parametrize("field", ["failures", "pin_deferrals"])
def test_new_execution_origin_cannot_invent_prior_failure_or_pin_history(origin, field):
    with pytest.raises(ValueError, match="fresh execution origin"):
        activated(origin=origin, **{field: 1})


@pytest.mark.parametrize("receipt", [None, "", " ", "line\nbreak", True, 123, "x" * 513])
def test_nonempty_checkpoint_receipt_identity_is_validated_when_supplied(receipt):
    component = activated()
    completed = project_review_execution(component.checkpoint, (failed(1),)).checkpoint
    if receipt is None:
        # An unverified candidate can exist in the pure domain; SQL loads require its receipt.
        assert replace(component, checkpoint=completed, state=completed.state, last_sequence=1,
                       checkpoint_receipt=None).checkpoint_receipt is None
    else:
        with pytest.raises(ValueError):
            replace(component, checkpoint=completed, state=completed.state, last_sequence=1, checkpoint_receipt=receipt)


def test_compaction_receipt_belongs_to_a_noninitial_checkpoint():
    component = activated()
    with pytest.raises(ValueError, match="initial execution checkpoint"):
        replace(component, checkpoint_receipt="checkpoint:verified")
    completed = project_review_execution(component.checkpoint, (failed(1),)).checkpoint
    compacted = replace(component, checkpoint=completed, state=completed.state, last_sequence=1,
                        checkpoint_receipt="checkpoint:verified")
    assert compacted.checkpoint_receipt == "checkpoint:verified" and compacted.state.failures == 1


@pytest.mark.parametrize("component", [None, "active", {}, 0])
def test_snapshot_and_family_envelope_require_an_explicit_typed_execution_component(component):
    state = composed_review()
    with pytest.raises(ValueError, match="typed execution component"):
        replace(state, execution=component)
    with pytest.raises(ValueError, match="typed execution component"):
        ReviewFamilyEpisodes(state.authority, state.episode, component)
