"""Durable checkpoint segment audit over retained round and generation sources."""

from dataclasses import replace

import pytest

from issue_agent.adapters.review_checkpoint_archive_audit import (
    advance_review_checkpoint_archive_audit,
)
from issue_agent.adapters.review_execution_activation import read_checkpoint_round_receipt_source
from issue_agent.adapters.review_execution_compaction import observe_review_checkpoint_manifest
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, review_execution_checkpoint_receipt_id,
)
from issue_agent.domain.review_checkpoint_archive import (
    BlockedReviewCheckpointArchiveAudit, CompletedReviewCheckpointArchiveAudit,
    ReviewCheckpointArchiveAwaiting, ReviewCheckpointArchiveSegment,
    ReviewCheckpointArchiveState, review_checkpoint_archive_overdue,
)
from issue_agent.domain.review_execution import (
    ActivatedReviewExecution, build_review_execution_checkpoint_receipt,
    plan_review_execution_compaction,
    project_review_execution,
)
from tests.adapters.test_review_execution_activation_sources import _accepted_generation
from tests.adapters.test_review_execution_control import rig
from tests.adapters.test_review_execution_persistence import (
    active, insert_round, round_facts, set_projection,
)


def _insert_receipt(db, receipt, now):
    receipt_id = review_execution_checkpoint_receipt_id(receipt)
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES ('review-execution-checkpoint','o/r',?,?,?)",
        (receipt_id, now.isoformat(), encode_workflow(receipt)),
    )
    return receipt_id


def _two_valid_checkpoints(active):
    store, snapshot = active
    first, first_outcome = round_facts(snapshot, 1)
    second, second_outcome = round_facts(snapshot, 2)
    key = snapshot.revision.fence.key
    with store.transaction() as db:
        insert_round(db, first, first_outcome)
        insert_round(db, second, second_outcome)
        projected = project_review_execution(
            snapshot.execution.checkpoint, (first_outcome, second_outcome),
        )
        complete = replace(snapshot, execution=replace(
            snapshot.execution, state=projected.state, last_sequence=2,
        ))
        first_plan = plan_review_execution_compaction(
            complete.execution, (first_outcome, second_outcome), 1,
        )
        first_source = read_checkpoint_round_receipt_source(db, first.plan.round)
        first_receipt = build_review_execution_checkpoint_receipt(
            complete.execution, first_plan, (first_source,), (),
        )
        first_id = _insert_receipt(db, first_receipt, store.now())
        after_first = replace(
            complete.execution, checkpoint=first_plan.checkpoint,
            checkpoint_receipt=first_id,
        )
        plan = plan_review_execution_compaction(after_first, (second_outcome,), 2)
        second_source = read_checkpoint_round_receipt_source(db, second.plan.round)
        second_receipt = build_review_execution_checkpoint_receipt(
            after_first, plan, (second_source,), (),
        )
        second_id = _insert_receipt(db, second_receipt, store.now())
        final = replace(complete, execution=replace(
            after_first, checkpoint=plan.checkpoint, state=plan.checkpoint.state,
            checkpoint_receipt=second_id,
        ))
        set_projection(db, final, (), 2)
    return store, key, first_id, second_id


def _append_checkpoint(store, snapshot, sequence):
    facts, outcome = round_facts(snapshot, sequence)
    with store.transaction() as db:
        insert_round(db, facts, outcome)
        from issue_agent.adapters.review_round_archive_audit import _current_execution
        execution = _current_execution(db, snapshot.revision.fence.key)
        projected = project_review_execution(execution.checkpoint, (outcome,))
        updated = replace(execution, state=projected.state, last_sequence=sequence)
        plan = plan_review_execution_compaction(updated, (outcome,), sequence)
        source = read_checkpoint_round_receipt_source(db, facts.plan.round)
        receipt = build_review_execution_checkpoint_receipt(updated, plan, (source,), ())
        receipt_id = _insert_receipt(db, receipt, store.now())
        row = db.execute(
            "SELECT episode_json FROM workflows WHERE repository='o/r' AND target_kind='pr'",
        ).fetchone()
        from issue_agent.domain.review_workflow import ReviewFamilyEpisodes
        episodes = decode_workflow(row["episode_json"], ReviewFamilyEpisodes)
        assert isinstance(episodes.execution, ActivatedReviewExecution)
        db.execute(
            "UPDATE workflows SET episode_json=? WHERE repository='o/r' AND target_kind='pr'",
            (encode_workflow(replace(episodes, execution=replace(
                updated, checkpoint=plan.checkpoint, state=plan.checkpoint.state,
                checkpoint_receipt=receipt_id,
            ))),),
        )
    return receipt_id


def _advance(store, key, now):
    with store.transaction() as db:
        return advance_review_checkpoint_archive_audit(db, key, now=now)


def _complete(store, key, now):
    for _ in range(32):
        state = _advance(store, key, now)
        if isinstance(state, (CompletedReviewCheckpointArchiveAudit,
                              BlockedReviewCheckpointArchiveAudit)):
            return state
    raise AssertionError("checkpoint audit did not complete in bounded steps")


def _stored(store):
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM level_state "
            "WHERE namespace='review-checkpoint-archive-scan'",
        ).fetchone()
        assert row is not None
        return decode_workflow(row["payload_json"], ReviewCheckpointArchiveState)


def test_two_checkpoint_segments_reach_origin_with_exact_streams(active):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    first = _advance(store, key, now)
    assert isinstance(first, ReviewCheckpointArchiveSegment)
    assert first.source.receipt_id == tip
    result = _complete(store, key, now)
    assert isinstance(result, CompletedReviewCheckpointArchiveAudit)
    assert result.source_count == 2 and result.tip_receipt_id == tip
    assert _stored(store) == result
    assert _advance(store, key, now) == result


def test_changed_round_source_blocks_and_persists(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    assert isinstance(_advance(store, key, now), ReviewCheckpointArchiveSegment)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-01-01T00:00:00+00:00' "
            "WHERE namespace='review-round-preflight' AND key='round:2'",
        )
    result = _complete(store, key, now)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert review_checkpoint_archive_overdue(result, now)
    assert _stored(store) == result


def test_prior_checkpoint_source_change_is_found_on_next_pass(active):
    store, key, first_id, _ = _two_valid_checkpoints(active)
    now = store.now()
    completed = _complete(store, key, now)
    assert isinstance(completed, CompletedReviewCheckpointArchiveAudit)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-07-30T12:00:00Z' "
            "WHERE namespace='review-execution-checkpoint' AND key=?",
            (first_id,),
        )
    result = _complete(store, key, completed.next_due_at)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert "changed since its prior pass" in result.reason


def test_new_tip_retains_prior_completed_chain_as_baseline(active):
    store, snapshot = active
    store, key, first_id, _ = _two_valid_checkpoints(active)
    now = store.now()
    completed = _complete(store, key, now)
    assert isinstance(completed, CompletedReviewCheckpointArchiveAudit)

    third_id = _append_checkpoint(store, snapshot, 3)
    extended = _complete(store, key, now)
    assert isinstance(extended, CompletedReviewCheckpointArchiveAudit)
    assert extended.tip_receipt_id == third_id and extended.source_count == 3

    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-07-30T12:00:00Z' "
            "WHERE namespace='review-execution-checkpoint' AND key=?",
            (first_id,),
        )
    result = _complete(store, key, extended.next_due_at)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert "changed since its prior pass" in result.reason


def test_missing_predecessor_blocks_on_its_page(active):
    store, key, first_id, _ = _two_valid_checkpoints(active)
    now = store.now()
    for _ in range(3):
        state = _advance(store, key, now)
    assert isinstance(state, ReviewCheckpointArchiveAwaiting)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE key=?", (first_id,))
    result = _advance(store, key, now)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert _stored(store) == result


def test_generation_and_round_segment_match_real_initial_checkpoint(rig):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    with store.transaction() as db:
        manifest = observe_review_checkpoint_manifest(db, key, 1)
        receipt_id = _insert_receipt(db, manifest.receipt, store.now())
        row = db.execute(
            "SELECT episode_json FROM workflows WHERE repository='o/r' AND target_kind='pr'",
        ).fetchone()
        from issue_agent.domain.review_workflow import ReviewFamilyEpisodes
        episodes = decode_workflow(row["episode_json"], ReviewFamilyEpisodes)
        assert isinstance(episodes.execution, ActivatedReviewExecution)
        updated = replace(episodes, execution=replace(
            episodes.execution, checkpoint=manifest.plan.checkpoint,
            state=manifest.plan.checkpoint.state, checkpoint_receipt=receipt_id,
            boundaries=(),
        ))
        db.execute(
            "UPDATE workflows SET episode_json=? WHERE repository='o/r' AND target_kind='pr'",
            (encode_workflow(updated),),
        )
    result = _complete(store, key, store.now())
    assert isinstance(result, CompletedReviewCheckpointArchiveAudit)
    assert result.source_count == 1


def test_malformed_persisted_cursor_refuses_resume(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _advance(store, key, now)
    with store.transaction() as db:
        db.execute(
            "UPDATE level_state SET payload_json='{}' "
            "WHERE namespace='review-checkpoint-archive-scan'",
        )
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="malformed"):
            advance_review_checkpoint_archive_audit(db, key, now=now)
