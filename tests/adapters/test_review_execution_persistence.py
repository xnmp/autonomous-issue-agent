"""Canonical execution projections over isolated stored round evidence.

SQL builds historical fixtures only. These tests exercise authoritative reads;
production admission and provenance minting remain a later checkpoint.
"""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib

import pytest

from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.control_store import _command_payload_digest, _json
from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
from issue_agent.adapters.workflow_rows import (
    REVIEW_STAGE_NAMESPACE, REVIEW_SUBMISSION_INDEX_NAMESPACE, REVIEW_SUBMISSION_NAMESPACE,
    WorkflowAuthorityError, encode_workflow_episodes, load_review_workflow_row,
    review_stage_receipt_key, review_submission_receipt_key,
)
from issue_agent.domain.contracts import CommandKind, LifecycleCommand
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.outcome import ExitReason
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.labels import Label
from issue_agent.domain.review_preflight import ReviewPreflightSelection
from issue_agent.domain.review_workflow import CompleteReviews, ObservedPullRequest, PullRequestState
from issue_agent.domain import review_execution as execution
from issue_agent.domain import review_rounds as rounds
from issue_agent.domain.workflow_identity import WorkflowRevision
from tests.adapters.test_control_store import _activate_execution_fixture, _seed_v24_workflow_history, _store


@pytest.fixture
def active(tmp_path):
    store = _store(tmp_path)
    _seed_v24_workflow_history(store)
    store.initialize()
    with store.transaction() as db:
        snapshot = _activate_execution_fixture(db)
    return store, snapshot


def round_facts(snapshot, sequence, *, copied=False):
    identity = execution.ReviewRoundIdentity(snapshot.execution.binding, f"round:{sequence}", sequence)
    submission = rounds.ReviewSubmissionPlan(f"submit:{sequence}", rounds.ReviewSubmissionKind.COPIED)
    child = execution.ReviewChildIdentity(identity, "review", "reviewer", execution.ReviewChildKind.REVIEWER)
    plan = rounds.ReviewRoundPlan(identity, "a" * 40, ReviewMode.SINGLE, RuntimeName.CLAUDE,
                                  rounds.ReviewSessionReuse.REUSABLE, 1, 5,
                                  () if copied else (rounds.ReviewStagePlan("review", child.kind, (child,)),),
                                  (submission,) if copied else ())
    sealed_payload = encode_workflow((identity.round_id, "fixture-owner", "claim:fixture",
                                      WorkflowRevision(identity.binding.rejection.fence, 0),
                                      plan, rounds.ReviewPlanningComplete()))
    planning = rounds.ReviewPlanningSealed(
        "review-round-seal:" + hashlib.sha256(sealed_payload.encode()).hexdigest(),
        rounds.ReviewPlanningComplete(),
    )
    children = () if copied else (rounds.ReviewChildReturned(
        execution.LaunchedReviewChild(child, f"run:{sequence}", f"launch:{sequence}"), f"return:{sequence}",
        rounds.ReviewChildFailed(ExitReason.AGENT_ERROR)),)
    submissions = (rounds.ReviewSubmissionEvidence(submission, rounds.ReviewSubmissionConfirmed(f"confirmed:{sequence}")),) if copied else ()
    facts = rounds.ReviewRoundFacts(f"closure:{sequence}", plan, planning, children, submissions)
    result = rounds.finalize_review_round(facts)
    assert isinstance(result, rounds.FinalizedReviewRound)
    return facts, result.outcome


def insert_seal_receipt(db, facts):
    identity = facts.plan.round
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES ('review-round-seal',?,?,'2026-09-27T00:00:00+00:00',?)",
        (identity.binding.rejection.fence.key.repository, identity.round_id,
         encode_workflow((identity.round_id, "fixture-owner", "claim:fixture",
                          WorkflowRevision(identity.binding.rejection.fence, 0), facts.plan, facts.planning))),
    )


def insert_round(db, facts, outcome=None, *, index_sequence=None):
    identity = facts.plan.round
    binding = identity.binding
    db.execute(
        "INSERT INTO review_execution_rounds(round_id,repository,target_kind,target_number,execution_epoch,"
        "execution_episode,generation,sequence,claim_occurrence_id,plan_json,planning_json,closure_json,outcome_json) "
        "VALUES (?,?,'pr',?,?,?,?,?,?,?,?,?,?)",
        (identity.round_id, binding.rejection.fence.key.repository, binding.rejection.fence.key.target.number,
         binding.execution_epoch, binding.execution_episode, binding.rejection.fence.generation,
         identity.sequence if index_sequence is None else index_sequence, "claim:fixture", encode_workflow(facts.plan),
         encode_workflow(facts.planning if outcome is not None else rounds.ReviewPlanningOpen()),
         encode_workflow(facts) if outcome is not None else None,
         encode_workflow(outcome) if outcome is not None else None),
    )
    for stage in facts.plan.stages:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES (?,?,?,'2026-09-27T00:00:00+00:00',?)",
            (REVIEW_STAGE_NAMESPACE, binding.rejection.fence.key.repository,
             review_stage_receipt_key(identity.round_id, stage.stage_id),
            encode_workflow((identity.round_id, stage))),
        )
    if facts.plan.submissions:
        initial = replace(facts.plan, stages=(), submissions=())
        selection = ReviewPreflightSelection(
            identity.round_id, WorkflowRevision(binding.rejection.fence, 0), binding,
            "claim:fixture", -binding.rejection.fence.key.target.number,
            ObservedPullRequest(binding.rejection.fence.key, facts.plan.head_sha,
                                PullRequestState.OPEN, frozenset({Label.AI_PR_OPEN}),
                                CompleteReviews(()), datetime(2026, 9, 27, tzinfo=timezone.utc)),
            Label.AI_PR_OPEN, facts.plan.mode, facts.plan.runtime, facts.plan.failure_ceiling,
        )
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-round-preflight',?,?,'2026-09-27T00:00:00+00:00',?)",
            (binding.rejection.fence.key.repository, identity.round_id,
             encode_workflow((selection, "fixture-owner", initial))),
        )
    for submission in facts.plan.submissions:
        op = BoundReviewSubmissionOp(
            identity.binding.rejection.fence.key.target.number, "APPROVE", "copied verdict",
            facts.plan.head_sha, "review-bot", "0" * 64, None,
        )
        command = LifecycleCommand(
            submission.command_id, f"github:{submission.command_id}", CommandKind.SUBMIT_REVIEW,
            f"pr:{op.pr}", op.head, op.payload(), "fixture", binding.rejection.fence.key.repository,
            "fixture-owner", workflow=binding.rejection,
        )
        payload = _json(command.payload)
        digest = _command_payload_digest(command, payload)
        db.execute(
            "INSERT INTO lifecycle_commands(command_id,idempotency_key,logical_key,occurrence,kind,target,"
            "expected_revision,config_version,repository,executor_identity,payload_json,payload_digest,"
            "state,created_at,updated_at,workflow_binding_json) "
            "VALUES (?,?,?,0,?,?,?,?,?,?,?,?,'succeeded','2026-09-27T00:00:00+00:00',"
            "'2026-09-27T00:00:00+00:00',?)",
            (submission.command_id, command.idempotency_key, command.idempotency_key,
             command.kind.value, command.target, command.expected_revision, command.config_version,
             command.repository, command.executor_identity, payload, digest, encode_workflow(command.workflow)),
        )
        registration = rounds.ReviewSubmissionRegistration(
            identity, submission, "claim:fixture", "fixture-owner",
            WorkflowRevision(binding.rejection.fence, 0), digest,
            command.idempotency_key, command.config_version,
        )
        for namespace, key in (
            (REVIEW_SUBMISSION_NAMESPACE, submission.command_id),
            (REVIEW_SUBMISSION_INDEX_NAMESPACE,
             review_submission_receipt_key(identity.round_id, submission.command_id)),
        ):
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES (?,?,?,'2026-09-27T00:00:00+00:00',?)",
                (namespace, binding.rejection.fence.key.repository, key, encode_workflow(registration)),
            )
    if outcome is not None:
        insert_seal_receipt(db, facts)


def set_projection(db, snapshot, outcomes, last_sequence):
    current = execution.project_review_execution(snapshot.execution.checkpoint, outcomes).state
    updated = replace(snapshot, execution=replace(snapshot.execution, state=current, last_sequence=last_sequence))
    db.execute("UPDATE workflows SET episode_json=? WHERE target_kind='pr'", (encode_workflow_episodes(updated),))
    return updated


def insert_checkpoint_receipt(db, checkpoint, *, receipt="checkpoint:1", scope="o/r"):
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES ('review-execution-checkpoint',?,?,'2026-09-27T00:00:00+00:00',?)",
        (scope, receipt, encode_workflow(checkpoint)),
    )


def test_ordinary_failure_projection_preserves_rejection_baseline(active):
    _, snapshot = active
    _, outcome = round_facts(snapshot, 1)
    projected = execution.project_review_execution(snapshot.execution.checkpoint, (outcome,))
    assert projected.state.failures == 4
    assert projected.state.binding == snapshot.execution.binding


def test_budget_exhaustion_projection_is_non_striking(active):
    _, snapshot = active
    original, _ = round_facts(snapshot, 1)
    failed = original.children[0]
    assert isinstance(failed, rounds.ReviewChildReturned)
    exhausted = replace(failed, result=rounds.ReviewChildBudgetExhausted())
    facts = replace(original, children=(exhausted,))
    result = rounds.finalize_review_round(facts)
    assert isinstance(result, rounds.FinalizedReviewRound)
    assert result.outcome.disposition == execution.ReviewBudgetExhausted((exhausted.launched,), facts.closure_receipt)
    projected = execution.project_review_execution(snapshot.execution.checkpoint, (result.outcome,))
    assert projected.state.failures == snapshot.execution.state.failures


@pytest.mark.parametrize("corruption", ["missing", "changed_plan", "changed_seal_id", "orphan_open",
                                        "future_revision"])
def test_canonical_execution_loader_requires_exact_completed_planning_seal(active, corruption):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1)
    with store.transaction() as db:
        if corruption == "orphan_open":
            insert_round(db, facts)
            insert_seal_receipt(db, facts)
            set_projection(db, snapshot, (), 1)
        else:
            insert_round(db, facts, outcome)
            set_projection(db, snapshot, (outcome,), 1)
            if corruption == "missing":
                db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-seal'")
            elif corruption == "changed_plan":
                db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-round-seal'",
                           (encode_workflow((facts.plan.round.round_id, "fixture-owner", "claim:fixture",
                                             WorkflowRevision(facts.plan.round.binding.rejection.fence, 0),
                                             replace(facts.plan, head_sha="b" * 40), facts.planning)),))
            elif corruption == "changed_seal_id":
                assert isinstance(facts.planning, rounds.ReviewPlanningSealed)
                changed = replace(facts.planning, seal_receipt="different-seal")
                db.execute("UPDATE review_execution_rounds SET planning_json=?",
                           (encode_workflow(changed),))
            else:
                future = WorkflowRevision(facts.plan.round.binding.rejection.fence, snapshot.revision.version + 100)
                body = encode_workflow((facts.plan.round.round_id, "fixture-owner", "claim:fixture",
                                        future, facts.plan, rounds.ReviewPlanningComplete()))
                changed = rounds.ReviewPlanningSealed(
                    "review-round-seal:" + hashlib.sha256(body.encode()).hexdigest(),
                    rounds.ReviewPlanningComplete(),
                )
                db.execute("UPDATE review_execution_rounds SET planning_json=?,closure_json=?",
                           (encode_workflow(changed), encode_workflow(replace(facts, planning=changed))))
                db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-round-seal'",
                           (encode_workflow((facts.plan.round.round_id, "fixture-owner", "claim:fixture",
                                             future, facts.plan, changed)),))
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="revision" if corruption == "future_revision" else None):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("corruption", ["missing_receipt", "orphan_receipt"])
def test_execution_authority_requires_the_complete_accepted_stage_set(active, corruption):
    store, snapshot = active
    facts, _ = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts)
        expected = set_projection(db, snapshot, (), 1)
        assert load_review_workflow_row(db, snapshot.revision.fence.key) == expected
        if corruption == "missing_receipt":
            db.execute("DELETE FROM dedupe_keys WHERE namespace=?", (REVIEW_STAGE_NAMESPACE,))
        else:
            db.execute("UPDATE review_execution_rounds SET plan_json=? WHERE round_id=?",
                       (encode_workflow(replace(facts.plan, stages=())), facts.plan.round.round_id))
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before


def test_out_of_order_projection_preserves_later_failure_across_earlier_copied_progress(active):
    _, snapshot = active
    _, progress = round_facts(snapshot, 1, copied=True)
    _, failed = round_facts(snapshot, 2)
    pending_earlier = execution.project_review_execution(snapshot.execution.checkpoint, (failed,))
    assert pending_earlier.state.failures == 4
    projected = execution.project_review_execution(snapshot.execution.checkpoint, (failed, progress))
    assert projected.state.failures == 1
    assert projected.state.pin_deferrals == 0
    assert projected.checkpoint.through_sequence == 2


@pytest.mark.parametrize("corruption", ["missing_round", "missing_closure", "missing_outcome", "altered_outcome",
                                         "open_closure", "missing_child_evidence", "projected_state",
                                         "planning_disagrees"])
def test_incomplete_or_conflicting_round_evidence_never_becomes_execution_authority(active, corruption):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        current = set_projection(db, snapshot, (outcome,), 1)
        if corruption == "missing_round":
            db.execute("DELETE FROM review_execution_rounds")
        elif corruption == "missing_closure":
            db.execute("UPDATE review_execution_rounds SET closure_json=NULL")
        elif corruption == "missing_outcome":
            db.execute("UPDATE review_execution_rounds SET outcome_json=NULL")
        elif corruption == "altered_outcome":
            altered = execution.ReviewRoundOutcome(facts.plan.round, execution.ReviewPinDeferred("different-disposition"))
            db.execute("UPDATE review_execution_rounds SET outcome_json=?", (encode_workflow(altered),))
        elif corruption == "open_closure":
            db.execute("UPDATE review_execution_rounds SET closure_json=?",
                       (encode_workflow(replace(facts, planning=rounds.ReviewPlanningOpen())),))
        elif corruption == "missing_child_evidence":
            db.execute("UPDATE review_execution_rounds SET closure_json=?", (encode_workflow(replace(facts, children=())),))
        elif corruption == "planning_disagrees":
            db.execute("UPDATE review_execution_rounds SET planning_json=?", (encode_workflow(rounds.ReviewPlanningOpen()),))
        else:
            altered = replace(current, execution=replace(current.execution, state=replace(current.execution.state, failures=99)))
            db.execute("UPDATE workflows SET episode_json=? WHERE target_kind='pr'", (encode_workflow_episodes(altered),))
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before


def test_open_round_plan_sequence_must_match_its_allocated_index(active):
    store, snapshot = active
    facts, _ = round_facts(snapshot, 2)
    with store.transaction() as db:
        insert_round(db, facts, index_sequence=1)
        set_projection(db, snapshot, (), 1)
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before


def test_missing_parent_is_not_absence_while_round_and_component_authority_remain(active):
    import sqlite3
    from issue_agent.adapters.workflow_rows import load_workflow_read

    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        set_projection(db, snapshot, (outcome,), 1)
    # Corrupt the isolated fixture without cascading retained authority away.
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DELETE FROM workflows WHERE target_kind='pr'")
    with store.transaction() as db:
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError):
            load_workflow_read(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before


def test_compacted_checkpoint_cannot_claim_an_unsettled_open_round(active):
    store, snapshot = active
    facts, _ = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts)
        current = set_projection(db, snapshot, (), 1)
        checkpoint = replace(current.execution.checkpoint, through_sequence=1)
        corrupted = replace(current, execution=replace(current.execution, checkpoint=checkpoint, checkpoint_receipt="checkpoint:1"))
        insert_checkpoint_receipt(db, checkpoint)
        db.execute("UPDATE workflows SET episode_json=? WHERE target_kind='pr'", (encode_workflow_episodes(corrupted),))
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before


def test_verified_compacted_prefix_loads_without_replaying_it(active):
    store, snapshot = active
    first, failure = round_facts(snapshot, 1)
    checkpoint = execution.project_review_execution(snapshot.execution.checkpoint, (failure,)).checkpoint
    assert checkpoint.through_sequence == 1 and checkpoint.state.failures == 4
    with store.transaction() as db:
        insert_round(db, first, failure)
        insert_checkpoint_receipt(db, checkpoint)
        compacted = replace(snapshot, execution=replace(snapshot.execution, checkpoint=checkpoint, state=checkpoint.state,
                                                       last_sequence=1, checkpoint_receipt="checkpoint:1"))
        expected = set_projection(db, compacted, (), 1)
        before = tuple(db.iterdump())
        recovered = load_review_workflow_row(db, snapshot.revision.fence.key)
        assert recovered == expected
        assert recovered.execution.state.failures == 4
        assert recovered.execution.checkpoint.policy.free_pin_deferrals == 1
        assert load_review_workflow_row(db, snapshot.revision.fence.key) == recovered
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("copied,expected_failures", [(False, 5), (True, 0)])
def test_compacted_projection_applies_later_suffix_once(active, copied, expected_failures):
    _, snapshot = active
    _, failure = round_facts(snapshot, 1)
    checkpoint = execution.project_review_execution(snapshot.execution.checkpoint, (failure,)).checkpoint
    _, outcome = round_facts(snapshot, 2, copied=copied)
    projected = execution.project_review_execution(checkpoint, (outcome,))
    assert projected.state.failures == expected_failures
    assert projected.checkpoint.policy.free_pin_deferrals == 1


@pytest.mark.parametrize("corruption", ["missing_reference", "missing_receipt", "foreign_scope", "different_state", "different_policy"])
def test_compacted_checkpoint_requires_its_exact_scoped_typed_receipt(active, corruption):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1)
    checkpoint = execution.project_review_execution(snapshot.execution.checkpoint, (outcome,)).checkpoint
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        compacted = replace(snapshot, execution=replace(snapshot.execution, checkpoint=checkpoint, state=checkpoint.state,
                                                       last_sequence=1,
                                                       checkpoint_receipt=None if corruption == "missing_reference" else "checkpoint:1"))
        set_projection(db, compacted, (), 1)
        if corruption != "missing_receipt":
            receipt = checkpoint
            if corruption == "different_state":
                receipt = replace(checkpoint, state=replace(checkpoint.state, failures=0))
            elif corruption == "different_policy":
                receipt = replace(checkpoint, policy=execution.ReviewExecutionPolicy(99))
            insert_checkpoint_receipt(db, receipt, scope="other/repo" if corruption == "foreign_scope" else "o/r")
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="checkpoint"):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before
