"""Durable, observational round-row census over the real SQLite source table."""

import json
from contextlib import closing

import pytest

import issue_agent.adapters.review_execution_activation as review_activation
from issue_agent.adapters.review_round_archive_audit import advance_review_round_archive_audit
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.domain.review_round_archive import (
    BlockedReviewRoundArchiveScan, CompletedReviewRoundArchiveScan,
    ReviewRoundArchiveCursor, ReviewRoundArchiveScan,
    review_round_archive_scan_overdue,
)
from issue_agent.domain.review_execution_activation import MAX_SQL_ROWID
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from tests.adapters.test_review_execution_persistence import (
    active, insert_round, round_facts, set_projection,
)


def _two_open_rounds(active):
    store, snapshot = active
    first, _ = round_facts(snapshot, 1)
    second, _ = round_facts(snapshot, 2)
    with store.transaction() as db:
        insert_round(db, first)
        insert_round(db, second)
        set_projection(db, snapshot, (), 2)
    return store, snapshot, first, second


def _stored(store):
    with closing(store._connect()) as db:
        row = db.execute(
            "SELECT payload_json FROM level_state WHERE namespace='review-round-archive-scan'",
        ).fetchone()
        assert row is not None
        return decode_workflow(row["payload_json"], ReviewRoundArchiveCursor).state


def test_round_archive_audit_completes_empty_episode_and_reuses_durable_result(active):
    store, snapshot = active
    key = snapshot.revision.fence.key
    now = store.now()
    with store.transaction() as db:
        completed = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(completed, CompletedReviewRoundArchiveScan)
    assert completed.through_sequence == 0 and completed.upper_rowid is None
    assert _stored(store) == completed
    with store.transaction() as db:
        before = db.total_changes
        unchanged = advance_review_round_archive_audit(db, key, now=now)
        assert db.total_changes == before
    assert unchanged == completed


def test_round_archive_audit_resumes_small_pages_and_counts_exact_episode(active, monkeypatch):
    store, snapshot, _, _ = _two_open_rounds(active)
    key = snapshot.revision.fence.key
    now = store.now()
    monkeypatch.setattr(review_activation, "MAX_CHECKPOINT_ROUND_SWEEP_PAGE", 1)
    with store.transaction() as db:
        first = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(first, ReviewRoundArchiveScan)
    assert first.source_count == 1 and first.after_rowid is not None
    assert first.upper_rowid is not None and first.after_rowid < first.upper_rowid
    assert _stored(store) == first
    with store.transaction() as db:
        complete = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(complete, CompletedReviewRoundArchiveScan)
    assert complete.through_sequence == 2
    assert _stored(store) == complete


def test_round_archive_audit_persists_a_missing_tail_after_restart(active, monkeypatch):
    store, snapshot, _, second = _two_open_rounds(active)
    key = snapshot.revision.fence.key
    now = store.now()
    monkeypatch.setattr(review_activation, "MAX_CHECKPOINT_ROUND_SWEEP_PAGE", 1)
    with store.transaction() as db:
        first = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(first, ReviewRoundArchiveScan)
    with store.transaction() as db:
        db.execute("DELETE FROM review_execution_rounds WHERE round_id=?",
                   (second.plan.round.round_id,))
    with store.transaction() as db:
        blocked = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(blocked, BlockedReviewRoundArchiveScan)
    assert "lacks allocated" in blocked.reason
    assert review_round_archive_scan_overdue(blocked, now)
    assert _stored(store) == blocked


def test_round_archive_audit_detects_source_change_on_next_pass(active, monkeypatch):
    store, snapshot, first_round, _ = _two_open_rounds(active)
    key = snapshot.revision.fence.key
    now = store.now()
    monkeypatch.setattr(review_activation, "MAX_CHECKPOINT_ROUND_SWEEP_PAGE", 1)
    for _ in range(2):
        with store.transaction() as db:
            completed = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(completed, CompletedReviewRoundArchiveScan)
    with store.transaction() as db:
        db.execute(
            "UPDATE review_execution_rounds SET claim_occurrence_id='changed-claim' WHERE round_id=?",
            (first_round.plan.round.round_id,),
        )
    with store.transaction() as db:
        resumed = advance_review_round_archive_audit(db, key, now=completed.next_due_at)
    assert isinstance(resumed, ReviewRoundArchiveScan)
    with store.transaction() as db:
        blocked = advance_review_round_archive_audit(db, key, now=completed.next_due_at)
    assert isinstance(blocked, BlockedReviewRoundArchiveScan)
    assert "changed since its prior pass" in blocked.reason
    assert _stored(store) == blocked


def test_round_archive_audit_restarts_when_new_round_arrives(active, monkeypatch):
    store, snapshot, _, _ = _two_open_rounds(active)
    key = snapshot.revision.fence.key
    now = store.now()
    monkeypatch.setattr(review_activation, "MAX_CHECKPOINT_ROUND_SWEEP_PAGE", 1)
    with store.transaction() as db:
        first = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(first, ReviewRoundArchiveScan)
    third, _ = round_facts(snapshot, 3)
    with store.transaction() as db:
        insert_round(db, third)
        set_projection(db, snapshot, (), 3)
    with store.transaction() as db:
        restarted = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(restarted, ReviewRoundArchiveScan)
    assert restarted.through_sequence == 3
    with store.transaction() as db:
        second = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(second, ReviewRoundArchiveScan)
    with store.transaction() as db:
        next_pass = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(next_pass, CompletedReviewRoundArchiveScan)
    assert next_pass.through_sequence == 3
    assert _stored(store) == next_pass


def test_round_archive_audit_persists_malformed_later_page(active, monkeypatch):
    store, snapshot, _, second = _two_open_rounds(active)
    key = snapshot.revision.fence.key
    now = store.now()
    monkeypatch.setattr(review_activation, "MAX_CHECKPOINT_ROUND_SWEEP_PAGE", 1)
    with store.transaction() as db:
        first = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(first, ReviewRoundArchiveScan)
    with store.transaction() as db:
        db.execute(
            "UPDATE review_execution_rounds SET sequence=CAST(sequence AS BLOB) WHERE round_id=?",
            (second.plan.round.round_id,),
        )
    with store.transaction() as db:
        restarted = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(restarted, ReviewRoundArchiveScan)
    with store.transaction() as db:
        blocked = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(blocked, BlockedReviewRoundArchiveScan)
    assert blocked.source_rowid is not None
    assert _stored(store) == blocked


def test_round_archive_audit_persists_origin_source_change(active):
    store, snapshot = active
    key = snapshot.revision.fence.key
    now = store.now()
    with store.transaction() as db:
        completed = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(completed, CompletedReviewRoundArchiveScan)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-09-29T00:00:00+00:00' "
            "WHERE namespace='review-execution-origin' AND key=?",
            (completed.origin_receipt.evidence_id,),
        )
        blocked = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(blocked, BlockedReviewRoundArchiveScan)
    assert "origin source changed" in blocked.reason
    assert _stored(store) == blocked


def test_round_archive_audit_restarts_an_unversioned_completed_cursor(active):
    store, snapshot = active
    key = snapshot.revision.fence.key
    now = store.now()
    with store.transaction() as db:
        completed = advance_review_round_archive_audit(db, key, now=now)
        db.execute(
            "UPDATE level_state SET payload_json=? "
            "WHERE namespace='review-round-archive-scan'",
            (encode_workflow(completed),),
        )
    with store.transaction() as db:
        restarted = advance_review_round_archive_audit(db, key, now=now)
        row = db.execute(
            "SELECT payload_json FROM level_state "
            "WHERE namespace='review-round-archive-scan'",
        ).fetchone()
    assert isinstance(restarted, CompletedReviewRoundArchiveScan)
    assert decode_workflow(row["payload_json"], ReviewRoundArchiveCursor).state == restarted


def test_round_archive_audit_rechecks_unversioned_completed_membership(active, monkeypatch):
    store, snapshot, first, _ = _two_open_rounds(active)
    key = snapshot.revision.fence.key
    now = store.now()
    monkeypatch.setattr(review_activation, "MAX_CHECKPOINT_ROUND_SWEEP_PAGE", 1)
    for _ in range(2):
        with store.transaction() as db:
            completed = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(completed, CompletedReviewRoundArchiveScan)
    with store.transaction() as db:
        db.execute(
            "UPDATE level_state SET payload_json=? "
            "WHERE namespace='review-round-archive-scan'",
            (encode_workflow(completed),),
        )
        db.execute(
            "UPDATE review_execution_rounds SET claim_occurrence_id='changed-claim' "
            "WHERE round_id=?",
            (first.plan.round.round_id,),
        )
    with store.transaction() as db:
        resumed = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(resumed, ReviewRoundArchiveScan)
    with store.transaction() as db:
        blocked = advance_review_round_archive_audit(db, key, now=now)
    assert isinstance(blocked, BlockedReviewRoundArchiveScan)
    assert "changed since its prior pass" in blocked.reason


def test_round_archive_audit_preserves_an_unversioned_blocker(active):
    store, snapshot = active
    key = snapshot.revision.fence.key
    now = store.now()
    with store.transaction() as db:
        completed = advance_review_round_archive_audit(db, key, now=now)
        blocked = BlockedReviewRoundArchiveScan(completed, "prior discrepancy", None, now)
        db.execute(
            "UPDATE level_state SET payload_json=? "
            "WHERE namespace='review-round-archive-scan'",
            (encode_workflow(blocked),),
        )
    with store.transaction() as db:
        before = db.total_changes
        unchanged = advance_review_round_archive_audit(db, key, now=now)
        assert db.total_changes == before
    assert unchanged == blocked


@pytest.mark.parametrize("invalid", (True, -1, MAX_SQL_ROWID, "0"))
def test_round_archive_audit_rejects_malformed_source_version(active, invalid):
    store, snapshot = active
    key = snapshot.revision.fence.key
    now = store.now()
    with store.transaction() as db:
        advance_review_round_archive_audit(db, key, now=now)
        row = db.execute(
            "SELECT payload_json FROM level_state "
            "WHERE namespace='review-round-archive-scan'",
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["value"]["fields"]["round_rows_version"] = invalid
        db.execute(
            "UPDATE level_state SET payload_json=? "
            "WHERE namespace='review-round-archive-scan'",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )
        with pytest.raises(WorkflowAuthorityError, match="cursor is malformed"):
            advance_review_round_archive_audit(db, key, now=now)
