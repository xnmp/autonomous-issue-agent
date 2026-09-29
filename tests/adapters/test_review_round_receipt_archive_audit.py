"""Durable receipt census over finalized prefixes and global namespace rows."""

import json
from dataclasses import replace

import pytest

from issue_agent.adapters.review_round_receipt_archive_audit import (
    _audit_key, advance_review_round_receipt_archive_audit,
)
from issue_agent.adapters.review_execution_compaction import observe_review_checkpoint_manifest
from issue_agent.adapters.review_archive_source_versions import read_review_archive_source_versions
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.review_round_receipt_archive import (
    BlockedReviewRoundReceiptArchiveScan, BlockedReviewRoundReceiptArchiveSweep,
    CompletedReviewRoundReceiptArchiveAudit, CompletedReviewRoundReceiptArchiveScan,
    ReviewRoundReceiptArchiveCursor, ReviewRoundReceiptArchiveScan,
    ReviewRoundReceiptArchiveSweep,
    review_round_receipt_archive_overdue,
)
from issue_agent.domain.review_execution_activation import MAX_SQL_ROWID
from tests.adapters.test_review_execution_control import current, rig
from tests.adapters.test_review_productive_settlement import confirmed_copy, workflow_for


def _advance(store, key, now):
    with store.transaction() as db:
        return advance_review_round_receipt_archive_audit(db, key, now=now)


def _complete(store, key, now):
    for _ in range(64):
        state = _advance(store, key, now)
        if isinstance(state, (CompletedReviewRoundReceiptArchiveAudit,
                              BlockedReviewRoundReceiptArchiveScan,
                              BlockedReviewRoundReceiptArchiveSweep)):
            return state
    raise AssertionError("bounded receipt audit did not complete")


def _stored(store):
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM level_state "
            "WHERE namespace='review-round-receipt-archive-scan-v2'",
        ).fetchone()
        assert row is not None
        return decode_workflow(row["payload_json"], ReviewRoundReceiptArchiveCursor).state


def test_finalized_round_expected_members_match_complete_namespace_pass(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    key = selection.expected.fence.key
    now = store.now()
    first = _advance(store, key, now)
    assert isinstance(first, CompletedReviewRoundReceiptArchiveScan)
    assert first.through_sequence == plan.round.sequence
    assert first.receipt_count >= 4
    with store.transaction() as db:
        manifest = observe_review_checkpoint_manifest(db, key, plan.round.sequence)
    assert first.rolling_sha256 == manifest.receipt.round_stream_sha256
    result = _complete(store, key, now)
    assert isinstance(result, CompletedReviewRoundReceiptArchiveAudit)
    assert result.receipt_count == first.receipt_count
    assert result.membership_xor == first.membership_xor
    assert _stored(store) == result
    assert _advance(store, key, now) == result


def test_prior_digest_format_cursor_is_preserved_and_new_pass_starts(rig):
    store, _, _, selection, _ = rig
    key = selection.expected.fence.key
    binding = current(store, selection).execution.binding
    audit_key = _audit_key(key, binding.execution_epoch, binding.execution_episode)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES (?,?,?,?,?)",
            ("review-round-receipt-archive-scan", key.repository,
             audit_key, "prior-format-evidence", store.now().isoformat()),
        )
    result = _advance(store, key, store.now())
    assert isinstance(result, CompletedReviewRoundReceiptArchiveAudit)
    with store.transaction() as db:
        rows = db.execute(
            "SELECT namespace,payload_json FROM level_state "
            "WHERE namespace LIKE 'review-round-receipt-archive-scan%' "
            "ORDER BY namespace",
        ).fetchall()
    assert rows[0]["namespace"] == "review-round-receipt-archive-scan"
    assert rows[0]["payload_json"] == "prior-format-evidence"
    assert rows[1]["namespace"] == "review-round-receipt-archive-scan-v2"
    assert decode_workflow(rows[1]["payload_json"], ReviewRoundReceiptArchiveCursor).state == result


def test_valid_open_round_receipt_is_pending_outside_finalized_prefix(rig):
    store, _, _, selection, control = rig
    control.preflight(selection)
    key = selection.expected.fence.key
    now = store.now()
    first = _advance(store, key, now)
    assert isinstance(first, CompletedReviewRoundReceiptArchiveAudit)
    assert first.expected.through_sequence == 0 and first.receipt_count == 0
    result = _complete(store, key, now)
    assert isinstance(result, CompletedReviewRoundReceiptArchiveAudit)
    assert result.pending_count == 1 and result.receipt_count == 0


def test_finalized_prefix_reconciles_while_a_later_round_remains_open(rig):
    store, gateway, selection, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    gateway.view = replace(gateway.view, head_ref_oid="c" * 40)
    later = replace(
        selection, request_id="preflight:later-pending",
        expected=current(store, selection).revision,
        observation=replace(selection.observation, head_sha="c" * 40),
    )
    control.preflight(later)
    result = _complete(store, selection.expected.fence.key, store.now())
    assert isinstance(result, CompletedReviewRoundReceiptArchiveAudit)
    assert result.expected.through_sequence == plan.round.sequence
    assert result.receipt_count == result.expected.receipt_count
    assert result.pending_count == 1


def test_open_round_finalization_during_namespace_pass_restarts_current_prefix(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    key = selection.expected.fence.key
    now = store.now()
    first = _advance(store, key, now)
    assert isinstance(first, ReviewRoundReceiptArchiveSweep)
    assert first.expected.through_sequence == 0
    workflow_for(store, gateway).settle_submission(command.command_id)
    completed = _complete(store, key, now)
    assert isinstance(completed, CompletedReviewRoundReceiptArchiveAudit)
    assert completed.expected.through_sequence == plan.round.sequence
    assert completed.receipt_count >= 4
    assert _advance(store, key, now) == completed


def test_changed_receipt_after_expected_pass_blocks_and_persists(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    key = selection.expected.fence.key
    now = store.now()
    assert isinstance(_advance(store, key, now), CompletedReviewRoundReceiptArchiveScan)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE namespace='review-round-preflight' AND key=?",
            (plan.round.round_id,),
        )
    result = _complete(store, key, now)
    assert isinstance(result, BlockedReviewRoundReceiptArchiveScan)
    assert "changed since its prior pass" in result.reason
    assert review_round_receipt_archive_overdue(result, now)
    assert _stored(store) == result


def test_missing_required_receipt_after_expected_pass_blocks(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    key = selection.expected.fence.key
    now = store.now()
    assert isinstance(_advance(store, key, now), CompletedReviewRoundReceiptArchiveScan)
    with store.transaction() as db:
        db.execute(
            "DELETE FROM dedupe_keys WHERE namespace='review-round-preflight' AND key=?",
            (plan.round.round_id,),
        )
    result = _complete(store, key, now)
    assert isinstance(result, BlockedReviewRoundReceiptArchiveScan)
    assert _stored(store) == result


def test_next_pass_detects_prior_finalized_receipt_mutation(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    key = selection.expected.fence.key
    now = store.now()
    complete = _complete(store, key, now)
    assert isinstance(complete, CompletedReviewRoundReceiptArchiveAudit)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE namespace='review-round-preflight' AND key=?",
            (plan.round.round_id,),
        )
    result = _advance(store, key, complete.next_due_at)
    assert isinstance(result, BlockedReviewRoundReceiptArchiveScan)
    assert "changed since its prior pass" in result.reason


def test_next_pass_detects_source_changed_after_its_sweep_page(rig):
    store, gateway, selection, _, _, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    key = selection.expected.fence.key
    now = store.now()
    assert isinstance(_advance(store, key, now), CompletedReviewRoundReceiptArchiveScan)
    first = _advance(store, key, now)
    assert isinstance(first, ReviewRoundReceiptArchiveSweep)
    assert first.after is not None
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE namespace=? AND scope=? AND key=?",
            (first.after.namespace, first.after.scope, first.after.key),
        )
    blocked = _complete(store, key, now)
    assert isinstance(blocked, BlockedReviewRoundReceiptArchiveScan)
    assert "changed since its prior pass" in blocked.reason


def test_unclaimed_recorded_receipt_blocks_namespace_pass(rig):
    store, _, _, selection, _ = rig
    key = selection.expected.fence.key
    revision = current(store, selection).revision
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-recorded-submission','o/r','command:orphan',"
            "'2026-09-29T00:00:00+00:00',?)",
            (encode_workflow(revision),),
        )
    result = _complete(store, key, store.now())
    assert isinstance(result, BlockedReviewRoundReceiptArchiveSweep)
    assert "absent or aliased identity" in result.reason


def test_blob_receipt_namespace_alias_persists_discrepancy(rig):
    store, _, _, selection, _ = rig
    key = selection.expected.fence.key
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES (CAST('review-round-preflight' AS BLOB),'o/r','round:alias',"
            "'2026-09-29T00:00:00+00:00','{}')",
        )
    result = _advance(store, key, store.now())
    assert isinstance(result, BlockedReviewRoundReceiptArchiveSweep)
    assert "BLOB alias" in result.reason
    assert _stored(store) == result


def test_malformed_or_aliased_persisted_cursor_refuses_resume(rig):
    store, _, _, selection, _ = rig
    key = selection.expected.fence.key
    now = store.now()
    assert isinstance(_advance(store, key, now), CompletedReviewRoundReceiptArchiveAudit)
    with store.transaction() as db:
        db.execute(
            "UPDATE level_state SET payload_json='{}' "
            "WHERE namespace='review-round-receipt-archive-scan-v2'",
        )
    with pytest.raises(WorkflowAuthorityError, match="malformed"):
        _advance(store, key, now)


def test_receipt_audit_restarts_an_unversioned_completed_cursor(rig):
    store, _, _, selection, _ = rig
    key = selection.expected.fence.key
    now = store.now()
    completed = _complete(store, key, now)
    assert isinstance(completed, CompletedReviewRoundReceiptArchiveAudit)
    with store.transaction() as db:
        db.execute(
            "UPDATE level_state SET payload_json=? "
            "WHERE namespace='review-round-receipt-archive-scan-v2'",
            (encode_workflow(completed),),
        )
    restarted = _complete(store, key, now)
    assert isinstance(restarted, CompletedReviewRoundReceiptArchiveAudit)
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM level_state "
            "WHERE namespace='review-round-receipt-archive-scan-v2'",
        ).fetchone()
    assert decode_workflow(row["payload_json"], ReviewRoundReceiptArchiveCursor).state == restarted


def test_receipt_audit_rechecks_unversioned_completed_membership(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    key = selection.expected.fence.key
    now = store.now()
    completed = _complete(store, key, now)
    assert isinstance(completed, CompletedReviewRoundReceiptArchiveAudit)
    with store.transaction() as db:
        db.execute(
            "UPDATE level_state SET payload_json=? "
            "WHERE namespace='review-round-receipt-archive-scan-v2'",
            (encode_workflow(completed),),
        )
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE namespace='review-round-preflight' AND key=?",
            (plan.round.round_id,),
        )
    blocked = _complete(store, key, now)
    assert isinstance(blocked, BlockedReviewRoundReceiptArchiveScan)
    assert "changed since its prior pass" in blocked.reason


def test_receipt_audit_restarts_after_foreign_round_row_changes(rig):
    store, gateway, selection, _, _, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    key = selection.expected.fence.key
    now = store.now()
    completed = _complete(store, key, now)
    assert isinstance(completed, CompletedReviewRoundReceiptArchiveAudit)
    with store.transaction() as db:
        before = read_review_archive_source_versions(db, key.repository)
        db.execute(
            "INSERT INTO workflows(repository,target_kind,target_number,generation,version,episode_json) "
            "SELECT 'other/repo',target_kind,target_number,generation,version,episode_json "
            "FROM workflows WHERE repository=? AND target_kind='pr'",
            (key.repository,),
        )
        db.execute(
            "INSERT INTO review_execution_rounds(round_id,repository,target_kind,target_number,"
            "execution_epoch,execution_episode,generation,sequence,claim_occurrence_id,"
            "plan_json,planning_json) "
            "SELECT 'foreign-round','other/repo',target_kind,target_number,execution_epoch,"
            "execution_episode,generation,sequence,claim_occurrence_id,plan_json,planning_json "
            "FROM review_execution_rounds WHERE repository=? ORDER BY sequence LIMIT 1",
            (key.repository,),
        )
        after = read_review_archive_source_versions(db, key.repository)
        assert (after.round_rows, after.round_receipts) == (before.round_rows, before.round_receipts)
        assert after.all_round_rows == before.all_round_rows + 1
    restarted = _advance(store, key, now)
    assert isinstance(restarted, CompletedReviewRoundReceiptArchiveScan)
    assert restarted.completed_at == now


def test_receipt_audit_preserves_an_unversioned_blocker(rig):
    store, _, _, selection, _ = rig
    key = selection.expected.fence.key
    now = store.now()
    completed = _complete(store, key, now)
    assert isinstance(completed, CompletedReviewRoundReceiptArchiveAudit)
    blocked = BlockedReviewRoundReceiptArchiveSweep(completed, "prior discrepancy", None, now)
    with store.transaction() as db:
        db.execute(
            "UPDATE level_state SET payload_json=? "
            "WHERE namespace='review-round-receipt-archive-scan-v2'",
            (encode_workflow(blocked),),
        )
    with store.transaction() as db:
        before = db.total_changes
        unchanged = advance_review_round_receipt_archive_audit(db, key, now=now)
        assert db.total_changes == before
    assert unchanged == blocked


@pytest.mark.parametrize("field", (
    "round_rows_version", "all_round_rows_version", "round_receipts_version",
))
@pytest.mark.parametrize("invalid", (True, -1, MAX_SQL_ROWID, "0"))
def test_receipt_audit_rejects_malformed_source_versions(rig, field, invalid):
    store, _, _, selection, _ = rig
    key = selection.expected.fence.key
    now = store.now()
    assert isinstance(_complete(store, key, now), CompletedReviewRoundReceiptArchiveAudit)
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM level_state "
            "WHERE namespace='review-round-receipt-archive-scan-v2'",
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["value"]["fields"][field] = invalid
        db.execute(
            "UPDATE level_state SET payload_json=? "
            "WHERE namespace='review-round-receipt-archive-scan-v2'",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )
        with pytest.raises(WorkflowAuthorityError, match="cursor is malformed"):
            advance_review_round_receipt_archive_audit(db, key, now=now)


@pytest.mark.parametrize("prior_pass", [False, True])
def test_malformed_open_frontier_persists_discrepancy(rig, prior_pass):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    key = selection.expected.fence.key
    now = store.now()
    if prior_pass:
        assert isinstance(_complete(store, key, now),
                          CompletedReviewRoundReceiptArchiveAudit)
    with store.transaction() as db:
        db.execute(
            "UPDATE review_execution_rounds SET sequence=CAST(sequence AS BLOB) "
            "WHERE round_id=?",
            (plan.round.round_id,),
        )
    result = _advance(store, key, now)
    assert isinstance(result, (BlockedReviewRoundReceiptArchiveScan,
                               BlockedReviewRoundReceiptArchiveSweep))
    assert "invalid open frontier" in result.reason
    assert _stored(store) == result
