"""Ordered outer review outcomes and continuation contracts; no runtime cutover."""

from dataclasses import FrozenInstanceError, replace
from itertools import permutations

import pytest

from issue_agent.domain.agent_task import RUNTIME_CAPABILITIES, RuntimeName
from issue_agent.domain.review_execution import (
    MAX_EXECUTION_SUFFIX, MAX_ROUND_CHILDREN, ActivatedReviewExecution, AddReviewStrike, ClearReviewSession,
    CopiedReviewProgress, ImportedReviewSession, LaunchedReviewChild, NoReviewSession,
    PaidReviewProgress, PreserveDisplayedRound, PreserveReviewSession, ReplaceReviewSession,
    ReusableReviewSession, ReviewAdmissionRefused, ReviewCancellationReason, ReviewChildIdentity,
    ReviewChildKind, ReviewExecutionBinding, ReviewExecutionCheckpoint, ReviewExecutionPolicy,
    ReviewExecutionOrigin, ReviewExecutionOriginKind, ReviewExecutionState, ReviewPinDeferred, ReviewPreparationDeferred, ReviewPreparationFailure,
    ReviewPreparationKind, ReviewReservationReleased, ReviewRoundCancelled, ReviewUnlaunchedRound,
    ReviewRoundFailure, ReviewRoundIdentity, ReviewRoundOutcome, SaturateReviewFailures,
    SetDisplayedRound, compatible_review_session, project_review_execution,
)
from issue_agent.domain.review_workflow import ReviewEpisode, ReviewFamilyEpisodes, ReviewWorkflowSnapshot
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
)


MAX_INTEGER = 2**63 - 1


def binding(generation=0):
    parent = WorkflowBinding(WorkflowFence(WorkflowKey("o/r", PullRequestRef(9)), generation),
                             FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1), "rejections:1")
    return ReviewExecutionBinding(parent, 2, "execution:1")


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
