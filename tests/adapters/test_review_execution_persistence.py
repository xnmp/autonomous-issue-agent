"""Canonical execution projections over isolated stored round evidence.

SQL builds historical fixtures only. These tests exercise authoritative reads;
production admission and provenance minting remain a later checkpoint.
"""

from dataclasses import replace

import pytest

from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import (
    REVIEW_STAGE_NAMESPACE, WorkflowAuthorityError, encode_workflow_episodes,
    load_review_workflow_row, review_stage_receipt_key,
)
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.outcome import ExitReason
from issue_agent.domain.review import ReviewMode
from issue_agent.domain import review_execution as execution
from issue_agent.domain import review_rounds as rounds
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
    planning = rounds.ReviewPlanningSealed(f"seal:{sequence}", rounds.ReviewPlanningComplete())
    children = () if copied else (rounds.ReviewChildReturned(
        execution.LaunchedReviewChild(child, f"run:{sequence}", f"launch:{sequence}"), f"return:{sequence}",
        rounds.ReviewChildFailed(ExitReason.TRANSPORT_DEATH)),)
    submissions = (rounds.ReviewSubmissionEvidence(submission, rounds.ReviewSubmissionConfirmed(f"confirmed:{sequence}")),) if copied else ()
    facts = rounds.ReviewRoundFacts(f"closure:{sequence}", plan, planning, children, submissions)
    result = rounds.finalize_review_round(facts)
    assert isinstance(result, rounds.FinalizedReviewRound)
    return facts, result.outcome


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


def test_one_stored_ordinary_failure_restores_one_outer_debit_and_exact_rejection_baseline(active):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        expected = set_projection(db, snapshot, (outcome,), 1)
        before = tuple(db.iterdump())
        restored = load_review_workflow_row(db, snapshot.revision.fence.key)
        assert restored == expected
        assert restored.execution.state.failures == 4
        assert restored.episode == snapshot.episode
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


def test_out_of_order_round_settlement_preserves_later_failure_across_earlier_copied_progress(active):
    store, snapshot = active
    early, progress = round_facts(snapshot, 1, copied=True)
    later, failed = round_facts(snapshot, 2)
    with store.transaction() as db:
        insert_round(db, early)
        insert_round(db, later, failed)
        set_projection(db, snapshot, (failed,), 2)
        assert load_review_workflow_row(db, snapshot.revision.fence.key).execution.state.failures == 4
        db.execute("UPDATE review_execution_rounds SET planning_json=?,closure_json=?,outcome_json=? WHERE round_id=?",
                   (encode_workflow(early.planning), encode_workflow(early), encode_workflow(progress), early.plan.round.round_id))
        set_projection(db, snapshot, (failed, progress), 2)
        before = tuple(db.iterdump())
        restored = load_review_workflow_row(db, snapshot.revision.fence.key)
        assert restored.execution.state.failures == 1
        assert restored.execution.state.pin_deferrals == 0
        assert restored.execution.checkpoint.through_sequence == 0
        assert restored.episode == snapshot.episode
        assert tuple(db.iterdump()) == before


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


@pytest.mark.parametrize("suffix,expected_failures", [(None, 4), ("failure", 5), ("copied", 0)])
def test_verified_compacted_prefix_loads_and_later_suffix_projects_once(active, suffix, expected_failures):
    store, snapshot = active
    first, failure = round_facts(snapshot, 1)
    checkpoint = execution.project_review_execution(snapshot.execution.checkpoint, (failure,)).checkpoint
    assert checkpoint.through_sequence == 1 and checkpoint.state.failures == 4
    with store.transaction() as db:
        insert_round(db, first, failure)
        insert_checkpoint_receipt(db, checkpoint)
        compacted = replace(snapshot, execution=replace(snapshot.execution, checkpoint=checkpoint, state=checkpoint.state,
                                                       last_sequence=1, checkpoint_receipt="checkpoint:1"))
        if suffix is None:
            expected = set_projection(db, compacted, (), 1)
        else:
            later, outcome = round_facts(snapshot, 2, copied=suffix == "copied")
            insert_round(db, later, outcome)
            expected = set_projection(db, compacted, (outcome,), 2)
        before = tuple(db.iterdump())
        recovered = load_review_workflow_row(db, snapshot.revision.fence.key)
        assert recovered == expected
        assert recovered.execution.state.failures == expected_failures
        assert recovered.execution.checkpoint.policy.free_pin_deferrals == 1
        assert load_review_workflow_row(db, snapshot.revision.fence.key) == recovered
        assert tuple(db.iterdump()) == before


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
