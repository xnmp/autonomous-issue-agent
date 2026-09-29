"""Durable checkpoint segment audit over retained round and generation sources."""

import json
from dataclasses import replace

import pytest

from issue_agent.adapters import review_checkpoint_archive_audit as archive_audit
from issue_agent.adapters.review_checkpoint_archive_audit import (
    _audit_key, advance_review_checkpoint_archive_audit,
)
from issue_agent.adapters.review_checkpoint_application_source import (
    REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, read_review_checkpoint_application_source,
)
from issue_agent.adapters.review_checkpoint_following_source import (
    read_checkpoint_following_application_source,
)
from issue_agent.adapters.review_checkpoint_state_replay import (
    observe_review_checkpoint_state_replay,
)
from issue_agent.adapters.review_execution_activation import read_checkpoint_round_receipt_source
from issue_agent.adapters.review_execution_compaction import observe_review_checkpoint_manifest
from issue_agent.adapters.review_round_archive_audit import _current_execution
from issue_agent.adapters.workflow_codec import (
    MAX_WORKFLOW_PAYLOAD_BYTES, WorkflowEncodingError, decode_workflow, encode_workflow,
)
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, review_execution_checkpoint_receipt_id,
)
from issue_agent.domain.review_checkpoint_archive import (
    BlockedReviewCheckpointArchiveAudit, CompletedReviewCheckpointArchiveAudit,
    ReviewCheckpointArchiveAwaiting, ReviewCheckpointArchiveSegment,
    ReviewCheckpointArchivePendingBoundary,
    ReviewCheckpointArchiveReplaySegment,
    ReviewCheckpointArchiveStorageAnchor,
    ReviewCheckpointArchiveState, begin_review_checkpoint_archive_scan,
    review_checkpoint_archive_overdue,
)
from issue_agent.domain.review_checkpoint_chain import ReviewCheckpointChainCursor
from issue_agent.domain.review_checkpoint_application import (
    ApplyAllCheckpointTailOutcomes, ApplyCheckpointTailByFollowingBoundary,
    ReviewCheckpointApplicationReceipt,
    application_receipt_from_proposal, build_review_checkpoint_application_proposal,
)
from issue_agent.domain.review_execution import (
    ActivatedReviewExecution, ReviewExecutionGenerationBoundary,
    ReviewExecutionBoundaryCursor, ReviewGenerationArchiveFingerprint,
    build_review_execution_checkpoint_receipt, carry_review_execution_generation,
    plan_review_execution_compaction,
    project_review_execution,
)
from issue_agent.domain.workflow import WorkflowMutation
from issue_agent.domain.workflow_identity import WorkflowRevision
from tests.adapters.test_review_execution_activation_sources import (
    _accepted_generation, _stale_settlement,
)
from tests.adapters.test_review_execution_control import rig
from tests.adapters.test_review_execution_persistence import (
    active, insert_round, round_facts, set_projection,
)


def _insert_receipt(db, receipt, now, application=None):
    receipt_id = review_execution_checkpoint_receipt_id(receipt)
    recorded = (
        application_receipt_from_proposal(receipt_id, application)
        if application is not None else ReviewCheckpointApplicationReceipt(
            receipt_id,
            receipt.checkpoint.boundary_cursor.revision
            if receipt.checkpoint.boundary_cursor is not None
            else WorkflowRevision(receipt.checkpoint.state.binding.rejection.fence, 0),
            receipt.previous_checkpoint.through_sequence,
            ApplyAllCheckpointTailOutcomes(),
        )
    )
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES ('review-execution-checkpoint','o/r',?,?,?)",
        (receipt_id, now.isoformat(), encode_workflow(receipt)),
    )
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
        "VALUES (?,?,?,?,?)",
        (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, "o/r", receipt_id,
         now.isoformat(), encode_workflow(recorded)),
    )
    return receipt_id


def _old_tip_encoding(value):
    def strip_tip_source(item):
        if isinstance(item, list):
            return [strip_tip_source(entry) for entry in item]
        if isinstance(item, dict):
            return {
                name: strip_tip_source(entry) for name, entry in item.items()
                if name != "tip_application_source_sha256"
            }
        return item

    return json.dumps(
        strip_tip_source(json.loads(encode_workflow(value))),
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


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
    for _ in range(64):
        state = _advance(store, key, now)
        if isinstance(state, (CompletedReviewCheckpointArchiveAudit,
                              BlockedReviewCheckpointArchiveAudit)):
            return state
    raise AssertionError("checkpoint audit did not complete in bounded steps")


def _stored(store):
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM level_state "
            "WHERE namespace='review-checkpoint-archive-scan-v4'",
        ).fetchone()
        assert row is not None
        return decode_workflow(row["payload_json"], ReviewCheckpointArchiveState)


def test_two_checkpoint_segments_reach_origin_with_exact_streams(active):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    first = _advance(store, key, now)
    assert isinstance(first, ReviewCheckpointArchiveReplaySegment)
    assert first.segment.source.receipt_id == tip
    result = _complete(store, key, now)
    assert isinstance(result, CompletedReviewCheckpointArchiveAudit)
    assert result.source_count == 2 and result.tip_receipt_id == tip
    assert _stored(store) == result
    assert _advance(store, key, now) == result


def test_changed_round_source_blocks_and_persists(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    assert isinstance(_advance(store, key, now), ReviewCheckpointArchiveReplaySegment)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-01-01T00:00:00+00:00' "
            "WHERE namespace='review-round-preflight' AND key='round:2'",
        )
    result = _complete(store, key, now)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert review_checkpoint_archive_overdue(result, now)
    assert _stored(store) == result


def test_missing_application_source_blocks_checkpoint_page(active):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    with store.transaction() as db:
        db.execute(
            "DELETE FROM dedupe_keys WHERE namespace=? AND key=?",
            (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, tip),
        )
    result = _advance(store, key, now)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert "checkpoint application source" in result.reason
    assert _stored(store) == result


def test_following_source_refuses_a_foreign_execution_origin(active):
    store, key, _, tip = _two_valid_checkpoints(active)
    with store.transaction() as db:
        execution = _current_execution(db, key)
        application = read_review_checkpoint_application_source(
            db, key.repository, tip,
        ).application
        origin_binding = application.previous_checkpoint.state.binding
        foreign_episode = replace(
            origin_binding,
            execution_epoch=execution.binding.execution_epoch + 1,
        )
        future_origin = replace(
            origin_binding,
            rejection=replace(
                origin_binding.rejection,
                fence=replace(origin_binding.rejection.fence,
                              generation=origin_binding.rejection.fence.generation + 1),
            ),
        )
        for foreign in (foreign_episode, future_origin):
            with pytest.raises(ValueError, match="same execution episode"):
                read_checkpoint_following_application_source(
                    db, foreign, execution.binding.rejection.fence.generation,
                    application,
                )


def test_prior_cursor_version_starts_new_application_aware_scan(active):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    with store.transaction() as db:
        execution = _current_execution(db, key)
        audit_key = _audit_key(
            key, execution.binding.execution_epoch, execution.binding.execution_episode,
        )
        cursor = ReviewCheckpointChainCursor(
            tip, execution.checkpoint, execution.origin,
            tip, execution.checkpoint, None,
        )
        prior = begin_review_checkpoint_archive_scan(cursor, "a" * 64, now, 3600)
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES ('review-checkpoint-archive-scan','o/r',?,?,?)",
            (audit_key, _old_tip_encoding(prior), now.isoformat()),
        )
    result = _advance(store, key, now)
    assert isinstance(result, ReviewCheckpointArchiveReplaySegment)
    assert result.segment.source.receipt_id == tip
    assert isinstance(_complete(store, key, now), CompletedReviewCheckpointArchiveAudit)


def test_prior_blocked_cursor_remains_a_persisted_blocker(active):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    with store.transaction() as db:
        execution = _current_execution(db, key)
        audit_key = _audit_key(
            key, execution.binding.execution_epoch, execution.binding.execution_episode,
        )
        cursor = ReviewCheckpointChainCursor(
            tip, execution.checkpoint, execution.origin,
            tip, execution.checkpoint, None,
        )
        prior = begin_review_checkpoint_archive_scan(cursor, "a" * 64, now, 3600)
        blocked = BlockedReviewCheckpointArchiveAudit(prior, "prior discrepancy", now)
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES ('review-checkpoint-archive-scan','o/r',?,?,?)",
            (audit_key, encode_workflow(blocked), now.isoformat()),
        )
    result = _advance(store, key, now)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert "prior checkpoint audit recorded" in result.reason
    assert _stored(store) == result
    assert _advance(store, key, now) == result


@pytest.mark.parametrize("prior_kind", ("valid", "blocked", "malformed"))
def test_v2_cursor_migrates_without_losing_prior_discrepancy(active, prior_kind):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    first = _advance(store, key, now)
    assert isinstance(first, ReviewCheckpointArchiveReplaySegment)
    prior = (
        BlockedReviewCheckpointArchiveAudit(first.segment, "prior discrepancy", now)
        if prior_kind == "blocked" else first.segment
    )
    with store.transaction() as db:
        row = db.execute(
            "SELECT key FROM level_state WHERE namespace='review-checkpoint-archive-scan-v4'",
        ).fetchone()
        db.execute(
            "DELETE FROM level_state WHERE namespace='review-checkpoint-archive-scan-v4'",
        )
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES ('review-checkpoint-archive-scan-v2','o/r',?,?,?)",
            (row["key"], "{}" if prior_kind == "malformed" else _old_tip_encoding(prior),
             now.isoformat()),
        )
    resumed = _advance(store, key, now)
    if prior_kind == "valid":
        assert isinstance(resumed, ReviewCheckpointArchiveReplaySegment)
        assert resumed.segment.source.receipt_id == tip
        assert isinstance(_complete(store, key, now), CompletedReviewCheckpointArchiveAudit)
    else:
        assert isinstance(resumed, BlockedReviewCheckpointArchiveAudit)
        assert "prior checkpoint audit" in resumed.reason
        assert _stored(store) == resumed
        assert _advance(store, key, now) == resumed


@pytest.mark.parametrize("prior_kind", ("valid", "blocked", "malformed"))
def test_v3_cursor_restarts_without_losing_prior_discrepancy(active, prior_kind):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    first = _advance(store, key, now)
    assert isinstance(first, ReviewCheckpointArchiveReplaySegment)
    prior = (
        BlockedReviewCheckpointArchiveAudit(first.segment, "prior discrepancy", now)
        if prior_kind == "blocked" else first
    )

    encoded = _old_tip_encoding(prior)
    with store.transaction() as db:
        row = db.execute(
            "SELECT key FROM level_state WHERE namespace='review-checkpoint-archive-scan-v4'",
        ).fetchone()
        db.execute(
            "DELETE FROM level_state WHERE namespace='review-checkpoint-archive-scan-v4'",
        )
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES ('review-checkpoint-archive-scan-v3','o/r',?,?,?)",
            (row["key"], "{}" if prior_kind == "malformed" else encoded, now.isoformat()),
        )
    resumed = _advance(store, key, now)
    if prior_kind == "valid":
        assert isinstance(resumed, ReviewCheckpointArchiveReplaySegment)
        assert resumed.segment.source.receipt_id == tip
        assert isinstance(_complete(store, key, now), CompletedReviewCheckpointArchiveAudit)
    else:
        assert isinstance(resumed, BlockedReviewCheckpointArchiveAudit)
        assert "prior checkpoint audit" in resumed.reason
        assert _stored(store) == resumed
        assert _advance(store, key, now) == resumed


def test_completed_v3_cursor_restarts_with_a_new_tip_digest(active):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    completed = _complete(store, key, now)
    assert isinstance(completed, CompletedReviewCheckpointArchiveAudit)
    with store.transaction() as db:
        row = db.execute(
            "SELECT key FROM level_state WHERE namespace='review-checkpoint-archive-scan-v4'",
        ).fetchone()
        db.execute(
            "DELETE FROM level_state WHERE namespace='review-checkpoint-archive-scan-v4'",
        )
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES ('review-checkpoint-archive-scan-v3','o/r',?,?,?)",
            (row["key"], _old_tip_encoding(completed), now.isoformat()),
        )
    restarted = _advance(store, key, now)
    assert isinstance(restarted, ReviewCheckpointArchiveReplaySegment)
    assert restarted.segment.source.receipt_id == tip
    renewed = _complete(store, key, now)
    assert isinstance(renewed, CompletedReviewCheckpointArchiveAudit)
    assert renewed.tip_application_source_sha256 == completed.tip_application_source_sha256


@pytest.mark.parametrize("damage", (
    "none", "source", "frontier", "nested_encoding",
))
def test_prior_segment_is_restarted_only_when_well_formed(active, damage):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    first = _advance(store, key, now)
    assert isinstance(first, ReviewCheckpointArchiveReplaySegment)
    encoded = json.loads(_old_tip_encoding(first.segment))
    fields = encoded["value"]["fields"]
    del fields["application"]
    del fields["application_source_sha256"]
    if damage == "source":
        fields["source"] = 123
    elif damage == "frontier":
        fields["next_round_sequence"] = 999999
    elif damage == "nested_encoding":
        instant = fields["parent"]["fields"]["started_at"]["value"]
        assert instant.endswith("+00:00")
        fields["parent"]["fields"]["started_at"]["value"] = (
            instant.removesuffix("+00:00") + "Z"
        )
    legacy_raw = json.dumps(
        encoded, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    with store.transaction() as db:
        row = db.execute(
            "SELECT key FROM level_state WHERE namespace='review-checkpoint-archive-scan-v4'",
        ).fetchone()
        db.execute(
            "DELETE FROM level_state WHERE namespace='review-checkpoint-archive-scan-v4'",
        )
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES ('review-checkpoint-archive-scan','o/r',?,?,?)",
            (row["key"], legacy_raw, now.isoformat()),
        )
    result = _advance(store, key, now)
    if damage != "none":
        assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
        assert "prior checkpoint audit cursor is malformed" in result.reason
    else:
        assert isinstance(result, ReviewCheckpointArchiveReplaySegment)
        assert result.segment.source.receipt_id == tip


def test_changed_application_source_blocks_segment_resume(active):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    assert isinstance(_advance(store, key, now), ReviewCheckpointArchiveReplaySegment)
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND key=?",
            (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, tip),
        ).fetchone()
        recorded = decode_workflow(row["payload_json"], ReviewCheckpointApplicationReceipt)
        changed = replace(recorded, captured_revision=replace(
            recorded.captured_revision, version=recorded.captured_revision.version + 1,
        ))
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? WHERE namespace=? AND key=?",
            (encode_workflow(changed), REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, tip),
        )
    result = _advance(store, key, now)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert "checkpoint application changed after its page" in result.reason
    assert _stored(store) == result


def test_selected_tail_requires_its_retained_following_source(active):
    store, snapshot = active
    key = snapshot.revision.fence.key
    first, first_outcome = round_facts(snapshot, 1)
    second, second_outcome = round_facts(snapshot, 2)
    before = project_review_execution(
        snapshot.execution.checkpoint, (second_outcome,),
    ).state
    next_fence = replace(snapshot.revision.fence, generation=snapshot.revision.fence.generation + 1)
    next_binding = replace(
        snapshot.execution.binding,
        rejection=replace(snapshot.execution.binding.rejection, fence=next_fence),
    )
    after = carry_review_execution_generation(before, next_binding, 2)
    boundary = ReviewExecutionGenerationBoundary(
        before, after, 2, (2,), "generation:late",
        snapshot.revision, WorkflowRevision(next_fence, snapshot.revision.version + 1),
        WorkflowMutation.REQUEST_REARM,
    )
    execution = replace(
        snapshot.execution, state=after, last_sequence=2, boundaries=(boundary,),
    )
    plan = plan_review_execution_compaction(
        execution, (first_outcome, second_outcome), 1,
    )
    assert plan.checkpoint.state == snapshot.execution.checkpoint.state
    with store.transaction() as db:
        insert_round(db, first, first_outcome)
        insert_round(db, second, second_outcome)
        source = read_checkpoint_round_receipt_source(db, first.plan.round)
        receipt = build_review_execution_checkpoint_receipt(execution, plan, (source,), ())
        following = plan.remaining_boundaries[0]
        source = ReviewGenerationArchiveFingerprint(
            ReviewExecutionBoundaryCursor(following.receipt_id, following.to_revision),
            "a" * 64, "workflow-request", "request:late", "b" * 64,
        )
        application = build_review_checkpoint_application_proposal(
            execution, plan, following.to_revision, source,
        )
        receipt_id = _insert_receipt(db, receipt, store.now(), application)
        row = db.execute(
            "SELECT episode_json FROM workflows WHERE repository='o/r' AND target_kind='pr'",
        ).fetchone()
        from issue_agent.domain.review_workflow import ReviewFamilyEpisodes
        episodes = decode_workflow(row["episode_json"], ReviewFamilyEpisodes)
        db.execute(
            "UPDATE workflows SET episode_json=?,generation=? "
            "WHERE repository='o/r' AND target_kind='pr'",
            (encode_workflow(replace(episodes, execution=replace(
                execution, checkpoint=plan.checkpoint, checkpoint_receipt=receipt_id,
            ))), next_fence.generation),
        )
    result = _complete(store, key, store.now())
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert "following generation" in result.reason


def _real_selected_checkpoint(rig):
    store, stale_plan, _, _ = _stale_settlement(rig)
    key = rig[1].revision.fence.key
    with store.transaction() as db:
        manifest = observe_review_checkpoint_manifest(db, key, stale_plan.round.sequence)
        receipt_id = _insert_receipt(
            db, manifest.receipt, store.now(), manifest.application,
        )
        row = db.execute(
            "SELECT episode_json FROM workflows WHERE repository='o/r' AND target_kind='pr'",
        ).fetchone()
        from issue_agent.domain.review_workflow import ReviewFamilyEpisodes
        episodes = decode_workflow(row["episode_json"], ReviewFamilyEpisodes)
        assert isinstance(episodes.execution, ActivatedReviewExecution)
        db.execute(
            "UPDATE workflows SET episode_json=? WHERE repository='o/r' AND target_kind='pr'",
            (encode_workflow(replace(episodes, execution=replace(
                episodes.execution, checkpoint=manifest.plan.checkpoint,
                checkpoint_receipt=receipt_id,
            ))),),
        )
    return store, key, manifest


def test_real_selected_tail_matches_first_retained_generation(rig):
    store, key, manifest = _real_selected_checkpoint(rig)
    assert manifest.plan.retired_boundaries == ()
    assert len(manifest.plan.remaining_boundaries) == 1
    result = _complete(store, key, store.now())
    assert isinstance(result, CompletedReviewCheckpointArchiveAudit)
    assert result.source_count == 1


def test_real_selected_checkpoint_state_replays_from_archived_sql(rig):
    store, key, manifest = _real_selected_checkpoint(rig)
    with store.transaction() as db:
        receipt_id = _current_execution(db, key).checkpoint_receipt
        assert receipt_id is not None
        before = db.total_changes
        observed = observe_review_checkpoint_state_replay(db, key, receipt_id)
        assert observed.checkpoint == manifest.plan.checkpoint
        assert observed.source_bytes > 0
        assert db.total_changes == before
        assert observe_review_checkpoint_state_replay(
            db, key, receipt_id, max_source_bytes=observed.source_bytes,
        ) == observed
        with pytest.raises(WorkflowAuthorityError, match="byte bound"):
            observe_review_checkpoint_state_replay(
                db, key, receipt_id, max_source_bytes=observed.source_bytes - 1,
            )


def test_two_historical_round_only_checkpoint_states_replay(active):
    store, key, first, tip = _two_valid_checkpoints(active)
    with store.transaction() as db:
        for receipt_id in (first, tip):
            observed = observe_review_checkpoint_state_replay(db, key, receipt_id)
            assert observed.checkpoint.through_sequence == (1 if receipt_id == first else 2)


def test_checkpoint_state_replay_blocks_changed_archived_outcome(active):
    store, key, _, tip = _two_valid_checkpoints(active)
    with store.transaction() as db:
        db.execute(
            "UPDATE review_execution_rounds SET outcome_json='{}' "
            "WHERE repository='o/r' AND sequence=2",
        )
    with store.transaction() as db:
        with pytest.raises((WorkflowAuthorityError, ValueError)):
            observe_review_checkpoint_state_replay(db, key, tip)


def test_changed_selected_following_source_blocks_segment_resume(rig):
    store, key, manifest = _real_selected_checkpoint(rig)
    now = store.now()
    assert isinstance(_advance(store, key, now), ReviewCheckpointArchiveReplaySegment)
    assert manifest.plan.remaining_boundaries
    assert isinstance(manifest.application.tail, ApplyCheckpointTailByFollowingBoundary)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-01-01T00:00:00+00:00' "
            "WHERE namespace=? AND key=?",
            (manifest.application.tail.source.accepted_namespace,
             manifest.application.tail.source.accepted_key),
        )
    result = _advance(store, key, now)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert "captured tail selection" in result.reason
    assert _stored(store) == result


def test_prior_checkpoint_source_change_is_found_on_next_pass(active):
    store, key, first_id, _ = _two_valid_checkpoints(active)
    now = store.now()
    completed = _complete(store, key, now)
    assert isinstance(completed, CompletedReviewCheckpointArchiveAudit)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-07-30T12:00:00Z' "
            "WHERE namespace IN ('review-execution-checkpoint',?) AND key=?",
            (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, first_id),
        )
    result = _complete(store, key, completed.next_due_at)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert "changed since its prior pass" in result.reason


def test_prior_application_source_change_is_found_on_next_pass(active):
    store, key, first_id, _ = _two_valid_checkpoints(active)
    now = store.now()
    completed = _complete(store, key, now)
    assert isinstance(completed, CompletedReviewCheckpointArchiveAudit)
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND key=?",
            (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, first_id),
        ).fetchone()
        recorded = decode_workflow(row["payload_json"], ReviewCheckpointApplicationReceipt)
        changed = replace(recorded, captured_revision=replace(
            recorded.captured_revision, version=recorded.captured_revision.version + 1,
        ))
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? WHERE namespace=? AND key=?",
            (encode_workflow(changed), REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, first_id),
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
            "WHERE namespace IN ('review-execution-checkpoint',?) AND key=?",
            (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, first_id),
        )
    result = _complete(store, key, extended.next_due_at)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert "changed since its prior pass" in result.reason


def test_missing_predecessor_blocks_on_its_page(active):
    store, key, first_id, _ = _two_valid_checkpoints(active)
    now = store.now()
    for _ in range(8):
        state = _advance(store, key, now)
        if isinstance(state, ReviewCheckpointArchiveAwaiting):
            break
    assert isinstance(state, ReviewCheckpointArchiveAwaiting)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE key=?", (first_id,))
    result = _advance(store, key, now)
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert _stored(store) == result


def _generation_checkpoint(rig):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    with store.transaction() as db:
        manifest = observe_review_checkpoint_manifest(db, key, 1)
        receipt_id = _insert_receipt(
            db, manifest.receipt, store.now(), manifest.application,
        )
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
    return store, key, receipt_id, manifest


def test_generation_and_round_segment_match_real_initial_checkpoint(rig):
    store, key, receipt_id, manifest = _generation_checkpoint(rig)
    with store.transaction() as db:
        observed = observe_review_checkpoint_state_replay(db, key, receipt_id)
        assert observed.checkpoint == manifest.plan.checkpoint
    first = _advance(store, key, store.now())
    assert isinstance(first, ReviewCheckpointArchiveReplaySegment)
    pending = _advance(store, key, store.now())
    assert isinstance(pending, ReviewCheckpointArchiveReplaySegment)
    assert isinstance(pending.replay, ReviewCheckpointArchivePendingBoundary)
    encoded = encode_workflow(pending)
    assert len(encoded.encode()) <= MAX_WORKFLOW_PAYLOAD_BYTES
    assert "ReviewExecutionGenerationBoundary" not in encoded
    result = _complete(store, key, store.now())
    assert isinstance(result, CompletedReviewCheckpointArchiveAudit)
    assert result.source_count == 1


def test_pending_generation_source_change_blocks_resume(rig):
    store, key, _, _ = _generation_checkpoint(rig)
    _advance(store, key, store.now())
    pending = _advance(store, key, store.now())
    assert isinstance(pending, ReviewCheckpointArchiveReplaySegment)
    assert isinstance(pending.replay, ReviewCheckpointArchivePendingBoundary)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-01-01T00:00:00+00:00' "
            "WHERE namespace=? AND key=?",
            (pending.replay.source.accepted_namespace, pending.replay.source.accepted_key),
        )
    result = _advance(store, key, store.now())
    assert isinstance(result, BlockedReviewCheckpointArchiveAudit)
    assert "pending generation source changed" in result.reason
    assert _stored(store) == result


@pytest.mark.parametrize("phase", ("first_page", "resume"))
def test_cursor_storage_excess_persists_a_bounded_blocker(active, monkeypatch, phase):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    first = _advance(store, key, now)
    assert isinstance(first, ReviewCheckpointArchiveReplaySegment)
    anchor = ReviewCheckpointArchiveStorageAnchor(
        first.segment.parent.cursor.tip_receipt_id,
        first.segment.parent.origin_source_sha256,
    )
    compact_blocker = BlockedReviewCheckpointArchiveAudit(
        anchor, "checkpoint archive audit cursor exceeds its storage bound", now,
    )
    limit = len(encode_workflow(compact_blocker).encode()) + 32
    assert len(encode_workflow(first).encode()) > limit
    if phase == "first_page":
        with store.transaction() as db:
            db.execute(
                "DELETE FROM level_state WHERE namespace='review-checkpoint-archive-scan-v4'",
            )
    original = archive_audit.encode_workflow

    def bounded(value):
        encoded = original(value)
        if (phase == "first_page" or value != first) and len(encoded.encode()) > limit:
            raise WorkflowEncodingError("workflow payload is too large")
        return encoded

    monkeypatch.setattr(archive_audit, "encode_workflow", bounded)
    blocked = _advance(store, key, now)
    assert isinstance(blocked, BlockedReviewCheckpointArchiveAudit)
    assert blocked.prior == anchor
    assert "storage bound" in blocked.reason
    assert _stored(store) == blocked
    assert _advance(store, key, now) == blocked


def test_prior_discrepancy_storage_excess_persists_anchor(active, monkeypatch):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    with store.transaction() as db:
        execution = _current_execution(db, key)
        audit_key = _audit_key(
            key, execution.binding.execution_epoch, execution.binding.execution_episode,
        )
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES ('review-checkpoint-archive-scan-v2','o/r',?,'{}',?)",
            (audit_key, now.isoformat()),
        )
    original = archive_audit.encode_workflow

    def bounded(value):
        if (isinstance(value, BlockedReviewCheckpointArchiveAudit)
                and isinstance(value.prior, ReviewCheckpointArchiveAwaiting)):
            raise WorkflowEncodingError("workflow payload is too large")
        return original(value)

    monkeypatch.setattr(archive_audit, "encode_workflow", bounded)
    blocked = _advance(store, key, now)
    assert isinstance(blocked, BlockedReviewCheckpointArchiveAudit)
    assert isinstance(blocked.prior, ReviewCheckpointArchiveStorageAnchor)
    assert "storage bound" in blocked.reason
    assert _stored(store) == blocked


def test_malformed_persisted_cursor_refuses_resume(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _advance(store, key, now)
    with store.transaction() as db:
        db.execute(
            "UPDATE level_state SET payload_json='{}' "
            "WHERE namespace='review-checkpoint-archive-scan-v4'",
        )
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="malformed"):
            advance_review_checkpoint_archive_audit(db, key, now=now)
