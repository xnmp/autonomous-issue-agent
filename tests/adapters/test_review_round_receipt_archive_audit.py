"""Durable receipt census over finalized prefixes and global namespace rows."""

from dataclasses import replace

import pytest

from issue_agent.adapters.review_round_receipt_archive_audit import (
    advance_review_round_receipt_archive_audit,
)
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.review_round_receipt_archive import (
    BlockedReviewRoundReceiptArchiveScan, BlockedReviewRoundReceiptArchiveSweep,
    CompletedReviewRoundReceiptArchiveAudit, CompletedReviewRoundReceiptArchiveScan,
    ReviewRoundReceiptArchiveScan, ReviewRoundReceiptArchiveSweep,
    review_round_receipt_archive_overdue,
)
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
            "WHERE namespace='review-round-receipt-archive-scan'",
        ).fetchone()
        assert row is not None
        return decode_workflow(
            row["payload_json"],
            ReviewRoundReceiptArchiveScan | CompletedReviewRoundReceiptArchiveScan
            | ReviewRoundReceiptArchiveSweep | CompletedReviewRoundReceiptArchiveAudit
            | BlockedReviewRoundReceiptArchiveScan | BlockedReviewRoundReceiptArchiveSweep,
        )


def test_finalized_round_expected_members_match_complete_namespace_pass(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    key = selection.expected.fence.key
    now = store.now()
    first = _advance(store, key, now)
    assert isinstance(first, CompletedReviewRoundReceiptArchiveScan)
    assert first.through_sequence == plan.round.sequence
    assert first.receipt_count >= 4
    result = _complete(store, key, now)
    assert isinstance(result, CompletedReviewRoundReceiptArchiveAudit)
    assert result.receipt_count == first.receipt_count
    assert result.membership_xor == first.membership_xor
    assert _stored(store) == result
    assert _advance(store, key, now) == result


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


def test_open_round_finalization_during_namespace_pass_waits_for_next_prefix(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    key = selection.expected.fence.key
    now = store.now()
    first = _advance(store, key, now)
    assert isinstance(first, ReviewRoundReceiptArchiveSweep)
    assert first.expected.through_sequence == 0
    workflow_for(store, gateway).settle_submission(command.command_id)
    completed = _complete(store, key, now)
    assert isinstance(completed, CompletedReviewRoundReceiptArchiveAudit)
    assert completed.receipt_count == 0
    next_pass = _advance(store, key, now)
    assert isinstance(next_pass, CompletedReviewRoundReceiptArchiveScan)
    assert next_pass.through_sequence == plan.round.sequence
    assert next_pass.receipt_count >= 4


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
    assert isinstance(result, BlockedReviewRoundReceiptArchiveSweep)
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
    assert isinstance(result, BlockedReviewRoundReceiptArchiveSweep)
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
    complete = _complete(store, key, now)
    assert isinstance(complete, CompletedReviewRoundReceiptArchiveAudit)
    blocked = _advance(store, key, complete.next_due_at)
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
            "WHERE namespace='review-round-receipt-archive-scan'",
        )
    with pytest.raises(WorkflowAuthorityError, match="malformed"):
        _advance(store, key, now)


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
