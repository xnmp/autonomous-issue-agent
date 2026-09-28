"""Canonical execution projections over isolated stored round evidence.

SQL builds historical fixtures only. These tests exercise authoritative reads;
production admission and provenance minting remain a later checkpoint.
"""

from dataclasses import replace
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import sqlite3

import pytest

from issue_agent.adapters.workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, encode_workflow
from issue_agent.adapters.review_execution_activation import (
    BlockedReviewCheckpointRoundPage, ObservedReviewCheckpointRoundPage,
    BlockedReviewCheckpointRoundSweepPage, ObservedReviewCheckpointRoundSweepPage,
    read_checkpoint_round_archive_page, read_checkpoint_round_by_sequence,
    read_checkpoint_round_receipt_source, read_checkpoint_round_row_source,
    read_checkpoint_round_sweep_page,
)
from issue_agent.adapters.review_execution_compaction import observe_review_checkpoint_manifest
from issue_agent.domain.review_execution_activation import MAX_LEGACY_ROUND_JSON_BYTES
from issue_agent.adapters.control_store import _command_payload_digest, _json
from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
from issue_agent.adapters.workflow_rows import (
    REVIEW_STAGE_NAMESPACE, REVIEW_SUBMISSION_INDEX_NAMESPACE, REVIEW_SUBMISSION_NAMESPACE,
    WorkflowAuthorityError, encode_workflow_episodes, load_review_workflow_row,
    review_execution_checkpoint_receipt_id, review_stage_receipt_key, review_submission_receipt_key,
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
from tests.adapters.test_control_store import (
    _activate_execution_fixture, _historical_fixture, _seed_v24_workflow_history, _store,
)


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
    seal = (facts.planning.completed_seal if isinstance(
        facts.planning, rounds.ReviewSealedParentAbandoned) else facts.planning)
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES ('review-round-seal',?,?,'2026-09-27T00:00:00+00:00',?)",
        (identity.binding.rejection.fence.key.repository, identity.round_id,
         encode_workflow((identity.round_id, "fixture-owner", "claim:fixture",
                          WorkflowRevision(identity.binding.rejection.fence, 0), facts.plan, seal))),
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
    if outcome is not None or facts.plan.submissions:
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
        completed = (facts.planning.completed_seal if isinstance(
            facts.planning, rounds.ReviewSealedParentAbandoned) else facts.planning)
        if isinstance(completed, rounds.ReviewPlanningSealed) and isinstance(
            completed.conclusion, rounds.ReviewPlanningComplete,
        ):
            insert_seal_receipt(db, facts)
        if isinstance(facts.planning, rounds.ReviewParentAbandoned):
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES ('review-round-abandonment',?,?,'2026-09-27T00:00:00+00:00',?)",
                (binding.rejection.fence.key.repository, identity.round_id,
                 encode_workflow((identity.round_id, "fixture-owner", "claim:fixture", facts.plan,
                                  facts.planning))),
            )
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-round-outcome',?,?,'2026-09-27T00:00:00+00:00',?)",
            (binding.rejection.fence.key.repository, identity.round_id, encode_workflow((facts, outcome))),
        )


def set_projection(db, snapshot, outcomes, last_sequence):
    current = execution.project_review_execution(snapshot.execution.checkpoint, outcomes).state
    updated = replace(snapshot, execution=replace(snapshot.execution, state=current, last_sequence=last_sequence))
    db.execute("UPDATE workflows SET episode_json=? WHERE target_kind='pr'", (encode_workflow_episodes(updated),))
    return updated


def checkpoint_receipt(db, snapshot, checkpoint, round_id):
    source = read_checkpoint_round_receipt_source(db, round_id)
    return execution.ReviewExecutionCheckpointReceipt(
        snapshot.execution.origin, snapshot.execution.checkpoint, None, checkpoint,
        "a" * 64, hashlib.sha256(b"").hexdigest(),
        source, None,
    )


def insert_checkpoint_receipt(db, receipt, *, key=None, scope="o/r"):
    receipt_id = key or review_execution_checkpoint_receipt_id(receipt)
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES ('review-execution-checkpoint',?,?,'2026-09-27T00:00:00+00:00',?)",
        (scope, receipt_id, encode_workflow(receipt)),
    )
    return receipt_id


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
        open_row = read_checkpoint_round_row_source(db, facts.plan.round)
        receipt_id = insert_checkpoint_receipt(db, execution.ReviewExecutionCheckpointReceipt(
            snapshot.execution.origin, snapshot.execution.checkpoint, None, checkpoint,
            "a" * 64, hashlib.sha256(b"").hexdigest(),
            execution.ReviewExecutionCompactionRoundSource(
                facts.plan.round, open_row.source_digest, "c" * 64,
            ), None,
        ))
        corrupted = replace(current, execution=replace(current.execution, checkpoint=checkpoint, checkpoint_receipt=receipt_id))
        db.execute("UPDATE workflows SET episode_json=? WHERE target_kind='pr'", (encode_workflow_episodes(corrupted),))
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before


def test_typed_compacted_prefix_refuses_load_without_verified_source_anchors(active):
    store, snapshot = active
    first, failure = round_facts(snapshot, 1)
    checkpoint = execution.project_review_execution(snapshot.execution.checkpoint, (failure,)).checkpoint
    assert checkpoint.through_sequence == 1 and checkpoint.state.failures == 4
    with store.transaction() as db:
        insert_round(db, first, failure)
        receipt_id = insert_checkpoint_receipt(db, checkpoint_receipt(db, snapshot, checkpoint, first.plan.round))
        compacted = replace(snapshot, execution=replace(snapshot.execution, checkpoint=checkpoint, state=checkpoint.state,
                                                       last_sequence=1, checkpoint_receipt=receipt_id))
        expected = set_projection(db, compacted, (), 1)
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="source anchors"):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert expected.execution.state.failures == 4
        assert tuple(db.iterdump()) == before
        db.execute(
            "UPDATE review_execution_rounds SET planning_json=? WHERE round_id=?",
            (encode_workflow(rounds.ReviewPlanningOpen()), first.plan.round.round_id),
        )
        tampered_before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="round row changed"):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == tampered_before


@pytest.mark.parametrize("corruption", [
    "missing_preflight", "changed_outcome", "aliased_seal", "extra_stage", "extra_index",
    "oversized_stage", "huge_stage_key", "blob_extra_stage",
])
def test_compacted_round_anchor_checks_exact_bounded_receipt_sources(active, corruption):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1)
    checkpoint = execution.project_review_execution(snapshot.execution.checkpoint, (outcome,)).checkpoint
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        source = read_checkpoint_round_receipt_source(db, facts.plan.round)
        assert source == read_checkpoint_round_receipt_source(db, facts.plan.round)
        receipt_id = insert_checkpoint_receipt(db, checkpoint_receipt(
            db, snapshot, checkpoint, facts.plan.round,
        ))
        compacted = replace(snapshot, execution=replace(
            snapshot.execution, checkpoint=checkpoint, state=checkpoint.state,
            last_sequence=1, checkpoint_receipt=receipt_id,
        ))
        set_projection(db, compacted, (), 1)
        if corruption == "missing_preflight":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-preflight'")
        elif corruption == "changed_outcome":
            db.execute(
                "UPDATE dedupe_keys SET claimed_at='2026-09-28T00:00:00+00:00' "
                "WHERE namespace='review-round-outcome'",
            )
        elif corruption == "aliased_seal":
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "SELECT CAST(namespace AS BLOB),scope,key,claimed_at,payload_json FROM dedupe_keys "
                "WHERE namespace='review-round-seal'",
            )
        elif corruption in {"extra_stage", "extra_index", "huge_stage_key", "blob_extra_stage"}:
            namespace = REVIEW_STAGE_NAMESPACE if corruption == "extra_stage" else REVIEW_SUBMISSION_INDEX_NAMESPACE
            if corruption in {"huge_stage_key", "blob_extra_stage"}:
                namespace = REVIEW_STAGE_NAMESPACE
            prefix = (review_stage_receipt_key(facts.plan.round.round_id, "review")[:-64]
                      if corruption == "extra_stage" else
                      review_submission_receipt_key(facts.plan.round.round_id, "missing")[:-64])
            source_key = prefix + "e" * (2**21 + 1 if corruption == "huge_stage_key" else 64)
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES (?,'o/r',CAST(? AS BLOB),'2026-09-27T00:00:00+00:00','{}')"
                if corruption == "blob_extra_stage" else
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES (?,'o/r',?,'2026-09-27T00:00:00+00:00','{}')",
                (namespace, source_key),
            )
        else:
            db.execute(
                "UPDATE dedupe_keys SET payload_json=? WHERE namespace=?",
                ("x" * (2**21 + 1), REVIEW_STAGE_NAMESPACE),
            )
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="checkpoint round"):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before


def test_compacted_productive_round_requires_one_settlement_source(active):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1, copied=True)
    assert isinstance(outcome.disposition, execution.CopiedReviewProgress)
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        with pytest.raises(WorkflowAuthorityError, match="checkpoint round receipt set"):
            read_checkpoint_round_receipt_source(db, facts.plan.round)
        command_id = facts.plan.submissions[0].command_id
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-recorded-submission','o/r',?,'2026-09-27T00:00:00+00:00',?)",
            (command_id, encode_workflow(WorkflowRevision(facts.plan.round.binding.rejection.fence, 0))),
        )
        source = read_checkpoint_round_receipt_source(db, facts.plan.round)
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-round-stale-productivity','o/r',?,'2026-09-27T00:00:00+00:00',?)",
            (facts.plan.round.round_id, encode_workflow((facts, outcome))),
        )
        with pytest.raises(WorkflowAuthorityError, match="checkpoint round receipt set"):
            read_checkpoint_round_receipt_source(db, facts.plan.round)
        db.execute(
            "DELETE FROM dedupe_keys WHERE namespace='review-round-stale-productivity'",
        )
        assert read_checkpoint_round_receipt_source(db, facts.plan.round) == source


def test_compacted_abandoned_round_binds_its_fence_receipt(active):
    store, snapshot = active
    original, _ = round_facts(snapshot, 1)
    abandoned = rounds.ReviewSealedParentAbandoned(
        "parent:dead", "late:fenced", original.planning,
    )
    facts = replace(original, planning=abandoned)
    result = rounds.finalize_review_round(facts)
    assert isinstance(result, rounds.FinalizedReviewRound)
    with store.transaction() as db:
        insert_round(db, facts, result.outcome)
        source = read_checkpoint_round_receipt_source(db, facts.plan.round)
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-abandonment'")
        with pytest.raises(WorkflowAuthorityError, match="checkpoint round receipt set"):
            read_checkpoint_round_receipt_source(db, facts.plan.round)
        assert source.receipt_set_sha256


def test_checkpoint_sequence_read_derives_retired_source_from_index(active):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        before = db.total_changes
        observed = read_checkpoint_round_by_sequence(db, snapshot.execution.binding, 1)
        assert observed.outcome == outcome
        assert observed.source == read_checkpoint_round_receipt_source(db, facts.plan.round)
        assert db.total_changes == before


@pytest.mark.parametrize("corruption", [
    "missing", "open", "oversized_outcome", "aliased_round_id", "aliased_episode", "foreign_binding",
    "changed_outcome_round",
])
def test_checkpoint_sequence_read_refuses_untrustworthy_round(active, corruption):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        binding = snapshot.execution.binding
        if corruption == "missing":
            db.execute("DELETE FROM review_execution_rounds WHERE round_id=?", (facts.plan.round.round_id,))
        elif corruption == "open":
            db.execute("UPDATE review_execution_rounds SET outcome_json=NULL WHERE round_id=?",
                       (facts.plan.round.round_id,))
        elif corruption == "oversized_outcome":
            db.execute("UPDATE review_execution_rounds SET outcome_json=? WHERE round_id=?",
                       ("x" * (MAX_LEGACY_ROUND_JSON_BYTES + 1), facts.plan.round.round_id))
        elif corruption == "aliased_round_id":
            db.execute("UPDATE review_execution_rounds SET round_id=CAST(round_id AS BLOB) WHERE round_id=?",
                       (facts.plan.round.round_id,))
        elif corruption == "aliased_episode":
            db.execute("UPDATE review_execution_rounds SET execution_episode=CAST(execution_episode AS BLOB) "
                       "WHERE round_id=?", (facts.plan.round.round_id,))
        elif corruption == "foreign_binding":
            binding = replace(binding, execution_episode="foreign-episode")
        else:
            changed = encode_workflow(outcome).replace("round:1", "foreign-round", 1)
            db.execute("UPDATE review_execution_rounds SET outcome_json=? WHERE round_id=?",
                       (changed, facts.plan.round.round_id))
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="checkpoint round sequence"):
            read_checkpoint_round_by_sequence(db, binding, 1)
        assert tuple(db.iterdump()) == before


def test_checkpoint_round_archive_pages_preserve_exact_sequence_and_detect_a_gap(active):
    store, snapshot = active
    first, first_outcome = round_facts(snapshot, 1)
    second, second_outcome = round_facts(snapshot, 2)
    with store.transaction() as db:
        insert_round(db, first, first_outcome)
        insert_round(db, second, second_outcome)
        binding = snapshot.execution.binding
        before = db.total_changes
        page = read_checkpoint_round_archive_page(db, binding, 1, 2, limit=1)
        assert isinstance(page, ObservedReviewCheckpointRoundPage)
        assert (page.rounds[0].outcome, page.next_sequence, page.has_more) == (
            first_outcome, 2, True,
        )
        final = read_checkpoint_round_archive_page(db, binding, page.next_sequence, 2)
        assert isinstance(final, ObservedReviewCheckpointRoundPage)
        assert (tuple(item.outcome for item in final.rounds), final.next_sequence, final.has_more) == (
            (second_outcome,), 3, False,
        )
        assert db.total_changes == before
        db.execute("DELETE FROM review_execution_rounds WHERE round_id=?", (second.plan.round.round_id,))
        blocked = read_checkpoint_round_archive_page(db, binding, 1, 2)
        assert isinstance(blocked, BlockedReviewCheckpointRoundPage)
        assert blocked.sequence == 2


def test_checkpoint_round_archive_page_rejects_invalid_bounds(active):
    store, snapshot = active
    with store.transaction() as db:
        for first, through, limit in ((0, 1, 1), (2, 1, 1), (1, 1, 0), (1, 1, 3)):
            with pytest.raises(ValueError, match="bounded episode range"):
                read_checkpoint_round_archive_page(
                    db, snapshot.execution.binding, first, through, limit=limit,
                )


@pytest.mark.parametrize("aliased", ["sequence", "execution_epoch"])
def test_checkpoint_sequence_read_rejects_duplicate_numeric_blob_alias(active, aliased):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        columns = (
            "round_id,repository,target_kind,target_number,execution_epoch,execution_episode,"
            "generation,sequence,claim_occurrence_id,plan_json,planning_json,closure_json,outcome_json"
        )
        selected = tuple(
            f"CAST({column} AS BLOB)" if column == aliased else column
            for column in columns.split(",")
        )
        selected = ("?", *selected[1:])
        db.execute(
            f"INSERT INTO review_execution_rounds({columns}) "
            f"SELECT {','.join(selected)} FROM review_execution_rounds WHERE round_id=?",
            ("alias:round", facts.plan.round.round_id),
        )
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="checkpoint round sequence"):
            read_checkpoint_round_by_sequence(db, snapshot.execution.binding, 1)
        assert tuple(db.iterdump()) == before


def test_checkpoint_manifest_refuses_an_open_retired_round(active):
    store, snapshot = active
    facts, _ = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts)
        set_projection(db, snapshot, (), 1)
        with pytest.raises(WorkflowAuthorityError, match="checkpoint manifest"):
            observe_review_checkpoint_manifest(db, snapshot.revision.fence.key, 1)


def test_checkpoint_round_sweep_pages_retain_a_fixed_repository_frontier(active):
    store, snapshot = active
    with store.transaction() as db:
        for sequence in range(1, 18):
            facts, _ = round_facts(snapshot, sequence)
            insert_round(db, facts)
        first = read_checkpoint_round_sweep_page(db, snapshot.execution.binding)
        assert isinstance(first, ObservedReviewCheckpointRoundSweepPage)
        assert len(first.sources) == 16 and first.has_more
        assert [dict(source.fields)["sequence"] for source in first.sources] == list(range(1, 17))
        facts, _ = round_facts(snapshot, 18)
        insert_round(db, facts)
        second = read_checkpoint_round_sweep_page(
            db, snapshot.execution.binding,
            after_rowid=first.next_rowid, upper_rowid=first.upper_rowid,
        )
        assert isinstance(second, ObservedReviewCheckpointRoundSweepPage)
        assert [dict(source.fields)["sequence"] for source in second.sources] == [17]
        assert not second.has_more and second.upper_rowid == first.upper_rowid
        fresh = read_checkpoint_round_sweep_page(db, snapshot.execution.binding)
        assert isinstance(fresh, ObservedReviewCheckpointRoundSweepPage)
        assert fresh.upper_rowid != first.upper_rowid
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT rowid FROM review_execution_rounds "
            "INDEXED BY review_execution_rounds_repository "
            "WHERE repository=? AND rowid<=? AND rowid>? ORDER BY rowid LIMIT 17",
            ("o/r", first.upper_rowid, first.next_rowid),
        ).fetchall()
        assert any("review_execution_rounds_repository" in row["detail"] for row in plan)


def test_checkpoint_round_sweep_accepts_a_smaller_bounded_page(active):
    store, snapshot = active
    with store.transaction() as db:
        for sequence in (1, 2):
            facts, _ = round_facts(snapshot, sequence)
            insert_round(db, facts)
        first = read_checkpoint_round_sweep_page(
            db, snapshot.execution.binding, limit=1,
        )
        assert isinstance(first, ObservedReviewCheckpointRoundSweepPage)
        assert len(first.sources) == 1 and first.has_more
        second = read_checkpoint_round_sweep_page(
            db, snapshot.execution.binding, after_rowid=first.next_rowid,
            upper_rowid=first.upper_rowid, limit=1,
        )
        assert isinstance(second, ObservedReviewCheckpointRoundSweepPage)
        assert [dict(source.fields)["sequence"] for source in second.sources] == [2]
        assert not second.has_more
        for invalid in (0, 17, True):
            with pytest.raises(ValueError, match="bounded cursor"):
                read_checkpoint_round_sweep_page(
                    db, snapshot.execution.binding, limit=invalid,
                )


def test_v26_round_sweep_index_migrates_without_losing_existing_rounds(active):
    store, snapshot = active
    facts, _ = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts)
    with closing(sqlite3.connect(store.path)) as db:
        _historical_fixture(db, 25)
        db.commit()
    store.initialize()
    with store.transaction() as db:
        page = read_checkpoint_round_sweep_page(db, snapshot.execution.binding)
        assert isinstance(page, ObservedReviewCheckpointRoundSweepPage)
        assert [dict(source.fields)["round_id"] for source in page.sources] == [
            facts.plan.round.round_id,
        ]


def test_checkpoint_round_sweep_keeps_negative_rowids_and_skips_other_episodes(active):
    store, snapshot = active
    first, _ = round_facts(snapshot, 1)
    second, _ = round_facts(snapshot, 2, copied=True)
    with store.transaction() as db:
        insert_round(db, first)
        insert_round(db, second)
        db.execute("UPDATE review_execution_rounds SET rowid=-7 WHERE round_id=?",
                   (first.plan.round.round_id,))
        foreign_binding = replace(snapshot.execution.binding, execution_episode="other-episode")
        foreign_plan = replace(second.plan, round=replace(second.plan.round, binding=foreign_binding))
        db.execute("UPDATE review_execution_rounds SET execution_episode='other-episode',plan_json=? "
                   "WHERE round_id=?", (encode_workflow(foreign_plan), second.plan.round.round_id))
        page = read_checkpoint_round_sweep_page(db, snapshot.execution.binding)
        assert isinstance(page, ObservedReviewCheckpointRoundSweepPage)
        assert [source.source_rowid for source in page.sources] == [-7]
        assert page.next_rowid > 0 and not page.has_more


def test_checkpoint_round_sweep_blocks_a_rekeyed_episode_with_old_plan(active):
    store, snapshot = active
    facts, _ = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts)
        db.execute("UPDATE review_execution_rounds SET execution_episode='other-episode' "
                   "WHERE round_id=?", (facts.plan.round.round_id,))
        blocked = read_checkpoint_round_sweep_page(db, snapshot.execution.binding)
        assert isinstance(blocked, BlockedReviewCheckpointRoundSweepPage)
        assert blocked.source_rowid is not None


@pytest.mark.parametrize("mutation", ("changed_outcome", "changed_planning"))
def test_checkpoint_round_sweep_checks_finalized_foreign_episode_before_filtering(active, mutation):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1, copied=True)
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        foreign_binding = replace(snapshot.execution.binding, execution_episode="other-episode")
        foreign_round = replace(facts.plan.round, binding=foreign_binding)
        foreign_plan = replace(facts.plan, round=foreign_round)
        foreign_facts = replace(facts, plan=foreign_plan)
        foreign_outcome = replace(outcome, round=foreign_round)
        if mutation == "changed_outcome":
            foreign_outcome = replace(
                foreign_outcome, disposition=execution.ReviewUnlaunchedRound("fake-closure"),
            )
            planning = foreign_facts.planning
        else:
            planning = rounds.ReviewPlanningOpen()
        db.execute(
            "UPDATE review_execution_rounds SET execution_episode=?,plan_json=?,"
            "planning_json=?,closure_json=?,outcome_json=? WHERE round_id=?",
            ("other-episode", encode_workflow(foreign_plan), encode_workflow(planning),
             encode_workflow(foreign_facts), encode_workflow(foreign_outcome),
             facts.plan.round.round_id),
        )
        blocked = read_checkpoint_round_sweep_page(db, snapshot.execution.binding)
        assert isinstance(blocked, BlockedReviewCheckpointRoundSweepPage)
        assert blocked.source_rowid is not None


@pytest.mark.parametrize("mutation", ("blob_repository", "blob_sequence", "noncanonical_blob_sequence"))
def test_checkpoint_round_sweep_blocks_malformed_repository_sources(active, mutation):
    store, snapshot = active
    facts, _ = round_facts(snapshot, 1)
    with store.transaction() as db:
        insert_round(db, facts)
    with closing(store._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        if mutation == "noncanonical_blob_sequence":
            db.execute(
                "UPDATE review_execution_rounds SET sequence=? WHERE round_id=?",
                (b"01", facts.plan.round.round_id),
            )
        else:
            column = "repository" if mutation == "blob_repository" else "sequence"
            db.execute(
                f"UPDATE review_execution_rounds SET {column}=CAST({column} AS BLOB) WHERE round_id=?",
                (facts.plan.round.round_id,),
            )
        before = db.total_changes
        blocked = read_checkpoint_round_sweep_page(db, snapshot.execution.binding)
        assert isinstance(blocked, BlockedReviewCheckpointRoundSweepPage)
        assert blocked.source_rowid is not None
        assert db.total_changes == before
        db.rollback()


def test_indexed_round_high_water_does_not_hide_a_missing_suffix_middle(active):
    store, snapshot = active
    first, _ = round_facts(snapshot, 1)
    third, _ = round_facts(snapshot, 3)
    with store.transaction() as db:
        insert_round(db, first)
        insert_round(db, third)
        allocated = replace(snapshot, execution=replace(snapshot.execution, last_sequence=3))
        db.execute(
            "UPDATE workflows SET episode_json=? WHERE target_kind='pr'",
            (encode_workflow_episodes(allocated),),
        )
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="round index"):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("corruption", ["missing", "blob_alias", "oversized_plan", "foreign_episode"])
def test_checkpoint_round_point_read_refuses_unreadable_or_foreign_rows(active, corruption):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1)
    identity = facts.plan.round
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        assert read_checkpoint_round_row_source(db, identity).source_digest
        if corruption == "missing":
            db.execute("DELETE FROM review_execution_rounds WHERE round_id=?", (identity.round_id,))
        elif corruption == "blob_alias":
            db.execute(
                "INSERT INTO review_execution_rounds "
                "SELECT CAST(round_id AS BLOB),repository,target_kind,target_number,execution_epoch,"
                "execution_episode,generation,sequence+1,claim_occurrence_id,plan_json,planning_json,"
                "closure_json,outcome_json FROM review_execution_rounds WHERE round_id=?",
                (identity.round_id,),
            )
        elif corruption == "oversized_plan":
            db.execute(
                "UPDATE review_execution_rounds SET plan_json=? WHERE round_id=?",
                ("x" * (MAX_LEGACY_ROUND_JSON_BYTES + 1), identity.round_id),
            )
        else:
            identity = replace(identity, binding=replace(identity.binding, execution_episode="foreign-episode"))
        with pytest.raises(WorkflowAuthorityError, match="checkpoint round row"):
            read_checkpoint_round_row_source(db, identity)


@pytest.mark.parametrize("copied,expected_failures", [(False, 5), (True, 0)])
def test_compacted_projection_applies_later_suffix_once(active, copied, expected_failures):
    _, snapshot = active
    _, failure = round_facts(snapshot, 1)
    checkpoint = execution.project_review_execution(snapshot.execution.checkpoint, (failure,)).checkpoint
    _, outcome = round_facts(snapshot, 2, copied=copied)
    projected = execution.project_review_execution(checkpoint, (outcome,))
    assert projected.state.failures == expected_failures
    assert projected.checkpoint.policy.free_pin_deferrals == 1


@pytest.mark.parametrize("corruption", [
    "missing_reference", "missing_receipt", "foreign_scope", "different_state",
    "different_policy", "foreign_origin", "wrong_key", "blob_alias",
    "oversized_payload", "naive_claim",
])
def test_compacted_checkpoint_requires_its_exact_scoped_typed_receipt(active, corruption):
    store, snapshot = active
    facts, outcome = round_facts(snapshot, 1)
    checkpoint = execution.project_review_execution(snapshot.execution.checkpoint, (outcome,)).checkpoint
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        receipt = checkpoint_receipt(db, snapshot, checkpoint, facts.plan.round)
        if corruption == "foreign_origin":
            receipt = replace(receipt, origin=execution.ReviewExecutionOrigin(
                execution.ReviewExecutionOriginKind.INITIALIZED, "foreign:origin",
            ))
        receipt_id = review_execution_checkpoint_receipt_id(receipt)
        compacted = replace(snapshot, execution=replace(snapshot.execution, checkpoint=checkpoint, state=checkpoint.state,
                                                       last_sequence=1,
                                                       checkpoint_receipt=None if corruption == "missing_reference" else receipt_id))
        set_projection(db, compacted, (), 1)
        if corruption != "missing_receipt":
            if corruption == "different_state":
                receipt = replace(receipt, checkpoint=replace(checkpoint, state=replace(checkpoint.state, failures=0)))
            elif corruption == "different_policy":
                receipt = checkpoint
            insert_checkpoint_receipt(
                db, receipt, key="checkpoint:wrong" if corruption == "wrong_key" else receipt_id,
                scope="other/repo" if corruption == "foreign_scope" else "o/r",
            )
        if corruption == "blob_alias":
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES (CAST(? AS BLOB),?,?,?,?)",
                ("review-execution-checkpoint", "o/r", receipt_id,
                 "2026-09-27T00:00:00+00:00", encode_workflow(receipt)),
            )
        elif corruption == "oversized_payload":
            db.execute(
                "UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-checkpoint' AND key=?",
                ("x" * (MAX_WORKFLOW_PAYLOAD_BYTES + 1), receipt_id),
            )
        elif corruption == "naive_claim":
            db.execute(
                "UPDATE dedupe_keys SET claimed_at='2026-09-27T00:00:00' "
                "WHERE namespace='review-execution-checkpoint' AND key=?", (receipt_id,),
            )
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="checkpoint"):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("predecessor", ["valid", "missing", "foreign_origin", "changed_frontier", "later_claim"])
def test_compacted_checkpoint_checks_its_immediate_predecessor(active, predecessor):
    store, snapshot = active
    first, first_outcome = round_facts(snapshot, 1)
    second, second_outcome = round_facts(snapshot, 2)
    first_checkpoint = execution.project_review_execution(
        snapshot.execution.checkpoint, (first_outcome,),
    ).checkpoint
    second_checkpoint = execution.project_review_execution(
        snapshot.execution.checkpoint, (first_outcome, second_outcome),
    ).checkpoint
    with store.transaction() as db:
        insert_round(db, first, first_outcome)
        insert_round(db, second, second_outcome)
        first_receipt = checkpoint_receipt(db, snapshot, first_checkpoint, first.plan.round)
        if predecessor == "foreign_origin":
            first_receipt = replace(first_receipt, origin=execution.ReviewExecutionOrigin(
                execution.ReviewExecutionOriginKind.INITIALIZED, "foreign:origin",
            ))
        elif predecessor == "changed_frontier":
            first_receipt = replace(first_receipt, checkpoint=replace(
                first_checkpoint, state=replace(first_checkpoint.state, failures=0),
            ))
        first_id = review_execution_checkpoint_receipt_id(first_receipt)
        second_receipt = execution.ReviewExecutionCheckpointReceipt(
            snapshot.execution.origin, first_checkpoint, first_id, second_checkpoint,
            "a" * 64, hashlib.sha256(b"").hexdigest(),
            read_checkpoint_round_receipt_source(db, second.plan.round), None,
        )
        if predecessor != "missing":
            insert_checkpoint_receipt(db, first_receipt)
        second_id = insert_checkpoint_receipt(db, second_receipt)
        if predecessor == "later_claim":
            db.execute(
                "UPDATE dedupe_keys SET claimed_at='2026-09-29T00:00:00+00:00' "
                "WHERE namespace='review-execution-checkpoint' AND key=?", (first_id,),
            )
        compacted = replace(snapshot, execution=replace(
            snapshot.execution, checkpoint=second_checkpoint, state=second_checkpoint.state,
            last_sequence=2, checkpoint_receipt=second_id,
        ))
        set_projection(db, compacted, (), 2)
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="source anchors" if predecessor == "valid" else "checkpoint"):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before
