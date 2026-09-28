"""Durable generation archive scans over real accepted transition sources."""

import sqlite3

import pytest

import issue_agent.adapters.review_execution_archive_audit as archive_audit
import issue_agent.adapters.review_execution_activation as review_activation
from issue_agent.adapters.review_execution_archive_audit import advance_review_generation_archive_audit
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.review_execution import (
    BlockedReviewGenerationArchiveScan, BlockedReviewGenerationArchiveSweep,
    CompletedReviewGenerationArchiveAudit, CompletedReviewGenerationArchiveScan,
    ReviewGenerationArchiveScan, ReviewGenerationArchiveSweep,
    review_generation_archive_scan_overdue,
)
from issue_agent.domain.workflow import (
    CancelRearm, ContinueGrantedRearm, OperatorActor, WorkflowCommitted,
)
from tests.adapters.test_review_execution_activation_sources import _accepted_generation
from tests.adapters.test_review_execution_control import current, rig
from tests.adapters.test_review_productive_settlement import accepted_generation_with_open_submission


def _two_generations(rig):
    store, _, selection, _, _, _, workflow = accepted_generation_with_open_submission(rig)
    first = current(store, selection)
    actor = OperatorActor("operator", "test")
    assert isinstance(workflow.cancel_rearm(
        CancelRearm("cancel:archive-audit", first.revision, "rearm:with-older-submission", "stop",
                    expected_binding=first.binding), actor,
    ), WorkflowCommitted)
    stopped = current(store, selection)
    assert isinstance(workflow.continue_rearm(
        ContinueGrantedRearm("rearm:archive-audit", stopped.revision,
                             "rearm:with-older-submission", expected_binding=stopped.binding), actor,
    ), WorkflowCommitted)
    return store, selection, workflow


def _stored_state(store):
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM level_state WHERE namespace='review-generation-archive-scan'",
        ).fetchone()
        assert row is not None
        return decode_workflow(
            row["payload_json"],
            ReviewGenerationArchiveScan | CompletedReviewGenerationArchiveScan
            | ReviewGenerationArchiveSweep | CompletedReviewGenerationArchiveAudit
            | BlockedReviewGenerationArchiveScan | BlockedReviewGenerationArchiveSweep,
        )


def test_generation_archive_scan_resumes_from_durable_small_cursor(rig, monkeypatch):
    store, selection, _ = _two_generations(rig)
    now = store.now()
    monkeypatch.setattr(archive_audit, "ARCHIVE_AUDIT_PAGE_SIZE", 1)

    with store.transaction() as db:
        first = advance_review_generation_archive_audit(db, selection.expected.fence.key, now=now)
    assert isinstance(first, ReviewGenerationArchiveScan)
    assert first.next_generation == first.origin.rejection.fence.generation + 2
    assert first.preceding is not None
    assert _stored_state(store) == first

    with store.transaction() as db:
        before = db.total_changes
        complete = advance_review_generation_archive_audit(db, selection.expected.fence.key, now=now)
        assert db.total_changes == before + 1
    assert isinstance(complete, CompletedReviewGenerationArchiveScan)
    assert complete.through_generation == first.origin.rejection.fence.generation + 2
    assert complete.rolling_sha256 != first.rolling_sha256
    assert _stored_state(store) == complete
    with store.transaction() as db:
        audited = advance_review_generation_archive_audit(db, selection.expected.fence.key, now=now)
    assert isinstance(audited, CompletedReviewGenerationArchiveAudit)
    assert audited.generation_scan == complete
    assert audited.source_count == 2
    assert audited.receipt_membership_xor == complete.receipt_membership_xor
    assert _stored_state(store) == audited
    with store.transaction() as db:
        unchanged = advance_review_generation_archive_audit(db, selection.expected.fence.key, now=now)
    assert unchanged == audited


def test_generation_archive_scan_finds_changed_source_on_next_pass(rig):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    now = store.now()
    with store.transaction() as db:
        generation_scan = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(generation_scan, CompletedReviewGenerationArchiveScan)
    with store.transaction() as db:
        complete = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(complete, CompletedReviewGenerationArchiveAudit)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET payload_json='{}' "
            "WHERE namespace='workflow-request' AND scope='o/r'",
        )
    with store.transaction() as db:
        blocked = advance_review_generation_archive_audit(db, key, now=complete.next_due_at)
    assert isinstance(blocked, BlockedReviewGenerationArchiveScan)
    assert "accepted transition" in blocked.reason
    assert review_generation_archive_scan_overdue(blocked, complete.next_due_at)
    assert _stored_state(store) == blocked


def test_generation_receipt_namespace_sweep_resumes_from_durable_cursor(rig, monkeypatch):
    store, selection, _ = _two_generations(rig)
    key = selection.expected.fence.key
    now = store.now()
    monkeypatch.setattr(review_activation, "MAX_GENERATION_RECEIPT_SWEEP_PAGE", 1)
    with store.transaction() as db:
        expected = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(expected, CompletedReviewGenerationArchiveScan)
    with store.transaction() as db:
        first = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(first, ReviewGenerationArchiveSweep)
    assert first.after is not None and first.upper is not None and first.after != first.upper
    assert first.source_count == 1
    assert _stored_state(store) == first
    with store.transaction() as db:
        complete = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(complete, CompletedReviewGenerationArchiveAudit)
    assert complete.source_count == 2
    assert complete.sweep_upper == first.upper
    assert _stored_state(store) == complete


def test_generation_receipt_sweep_persists_missing_tail_after_restart(rig, monkeypatch):
    store, selection, _ = _two_generations(rig)
    key = selection.expected.fence.key
    now = store.now()
    monkeypatch.setattr(review_activation, "MAX_GENERATION_RECEIPT_SWEEP_PAGE", 1)
    with store.transaction() as db:
        assert isinstance(advance_review_generation_archive_audit(db, key, now=now),
                          CompletedReviewGenerationArchiveScan)
    with store.transaction() as db:
        first = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(first, ReviewGenerationArchiveSweep)
    assert first.upper is not None and first.after != first.upper
    with store.transaction() as db:
        db.execute(
            "DELETE FROM dedupe_keys WHERE namespace='review-execution-generation' "
            "AND scope=? AND key=?", (first.upper.scope, first.upper.key),
        )
    with store.transaction() as db:
        blocked = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(blocked, BlockedReviewGenerationArchiveSweep)
    assert "disagrees with expected source pairs" in blocked.reason
    assert _stored_state(store) == blocked


def test_next_pass_detects_valid_source_mutation_after_its_sweep_page(rig, monkeypatch):
    store, selection, _ = _two_generations(rig)
    key = selection.expected.fence.key
    now = store.now()
    monkeypatch.setattr(review_activation, "MAX_GENERATION_RECEIPT_SWEEP_PAGE", 1)
    with store.transaction() as db:
        assert isinstance(advance_review_generation_archive_audit(db, key, now=now),
                          CompletedReviewGenerationArchiveScan)
    with store.transaction() as db:
        first = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(first, ReviewGenerationArchiveSweep)
    assert first.after is not None
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE namespace='review-execution-generation' AND scope=? AND key=?",
            (first.after.scope, first.after.key),
        )
    with store.transaction() as db:
        complete = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(complete, CompletedReviewGenerationArchiveAudit)
    with store.transaction() as db:
        blocked = advance_review_generation_archive_audit(db, key, now=complete.next_due_at)
    assert isinstance(blocked, BlockedReviewGenerationArchiveScan)
    assert "changed since its prior pass" in blocked.reason
    assert _stored_state(store) == blocked


def test_prior_digest_is_checked_when_generation_grows(rig):
    store, selection, workflow = _two_generations(rig)
    key = selection.expected.fence.key
    now = store.now()
    with store.transaction() as db:
        assert isinstance(advance_review_generation_archive_audit(db, key, now=now),
                          CompletedReviewGenerationArchiveScan)
    with store.transaction() as db:
        complete = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(complete, CompletedReviewGenerationArchiveAudit)

    before = current(store, selection)
    actor = OperatorActor("operator", "test")
    assert isinstance(workflow.cancel_rearm(
        CancelRearm("cancel:prior-digest", before.revision, "rearm:archive-audit",
                    "stop", expected_binding=before.binding), actor,
    ), WorkflowCommitted)
    stopped = current(store, selection)
    assert isinstance(workflow.continue_rearm(
        ContinueGrantedRearm("rearm:prior-digest", stopped.revision,
                             "rearm:archive-audit", expected_binding=stopped.binding), actor,
    ), WorkflowCommitted)
    prior_boundary = complete.generation_scan.final_cursor
    assert prior_boundary is not None
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE namespace='review-execution-generation' AND key=?",
            (prior_boundary.receipt_id,),
        )
    with store.transaction() as db:
        blocked = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(blocked, BlockedReviewGenerationArchiveScan)
    assert blocked.generation == complete.generation_scan.through_generation
    assert "changed since its prior pass" in blocked.reason


def test_generation_growth_during_sweep_finishes_captured_frontier(rig, monkeypatch):
    store, selection, workflow = _two_generations(rig)
    key = selection.expected.fence.key
    now = store.now()
    monkeypatch.setattr(review_activation, "MAX_GENERATION_RECEIPT_SWEEP_PAGE", 1)
    with store.transaction() as db:
        expected = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(expected, CompletedReviewGenerationArchiveScan)
    with store.transaction() as db:
        first = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(first, ReviewGenerationArchiveSweep)

    before = current(store, selection)
    actor = OperatorActor("operator", "test")
    assert isinstance(workflow.cancel_rearm(
        CancelRearm("cancel:during-audit-sweep", before.revision, "rearm:archive-audit",
                    "stop", expected_binding=before.binding), actor,
    ), WorkflowCommitted)
    stopped = current(store, selection)
    assert isinstance(workflow.continue_rearm(
        ContinueGrantedRearm("rearm:during-audit-sweep", stopped.revision,
                             "rearm:archive-audit", expected_binding=stopped.binding), actor,
    ), WorkflowCommitted)
    assert current(store, selection).revision.fence.generation == expected.through_generation + 1

    result = first
    for _ in range(3):
        with store.transaction() as db:
            result = advance_review_generation_archive_audit(db, key, now=now)
        if isinstance(result, CompletedReviewGenerationArchiveAudit):
            break
    assert isinstance(result, CompletedReviewGenerationArchiveAudit)
    assert result.generation_scan.through_generation == expected.through_generation
    with store.transaction() as db:
        resumed = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(resumed, CompletedReviewGenerationArchiveScan)
    assert resumed.through_generation == expected.through_generation + 1


def test_generation_receipt_sweep_blocks_changed_source_after_expected_pass(rig):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    now = store.now()
    with store.transaction() as db:
        expected = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(expected, CompletedReviewGenerationArchiveScan)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE namespace='review-execution-generation'",
        )
    with store.transaction() as db:
        blocked = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(blocked, BlockedReviewGenerationArchiveSweep)
    assert "disagrees with expected source pairs" in blocked.reason
    assert review_generation_archive_scan_overdue(blocked, now)
    assert _stored_state(store) == blocked


def test_generation_receipt_sweep_blocks_unclaimed_row_after_expected_pass(rig):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    now = store.now()
    with store.transaction() as db:
        expected = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(expected, CompletedReviewGenerationArchiveScan)
    with store.transaction() as db:
        row = db.execute(
            "SELECT namespace,scope,claimed_at,reusable_after,payload_json FROM dedupe_keys "
            "WHERE namespace='review-execution-generation'",
        ).fetchone()
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,reusable_after,payload_json) "
            "VALUES (?,?,?,?,?,?)",
            (row["namespace"], row["scope"], "unclaimed:generation",
             row["claimed_at"], row["reusable_after"], row["payload_json"]),
        )
    with store.transaction() as db:
        blocked = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(blocked, BlockedReviewGenerationArchiveSweep)
    assert blocked.source_rowid is not None
    assert "source identity" in blocked.reason
    assert _stored_state(store) == blocked


def test_generation_archive_scan_persists_a_retreating_indexed_frontier(rig):
    store, selection, _ = _two_generations(rig)
    key = selection.expected.fence.key
    now = store.now()
    with store.transaction() as db:
        complete = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(complete, CompletedReviewGenerationArchiveScan)
    with store.transaction() as db:
        db.execute(
            "UPDATE workflows SET generation=? WHERE repository=? AND target_number=?",
            (complete.through_generation - 1, key.repository, key.target.number),
        )
    with store.transaction() as db:
        blocked = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(blocked, BlockedReviewGenerationArchiveScan)
    assert blocked.reason == "indexed generation frontier moved backwards"
    assert _stored_state(store) == blocked


@pytest.mark.parametrize("change", ("DELETE", "UPDATE"))
def test_generation_archive_scan_blocks_changed_origin_before_next_due(rig, change):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    now = store.now()
    with store.transaction() as db:
        complete = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(complete, CompletedReviewGenerationArchiveScan)
    with store.transaction() as db:
        if change == "DELETE":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-origin'")
        else:
            db.execute(
                "UPDATE dedupe_keys SET claimed_at='2020-01-01T00:00:00+00:00' "
                "WHERE namespace='review-execution-origin'",
            )
    with store.transaction() as db:
        blocked = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(blocked, BlockedReviewGenerationArchiveScan)
    assert blocked.scan == complete
    assert blocked.reason == "execution origin source changed or is unreadable"
    assert _stored_state(store) == blocked


def test_generation_growth_preserves_incomplete_archive_pass(rig, monkeypatch):
    store, selection, workflow = _two_generations(rig)
    now = store.now()
    monkeypatch.setattr(archive_audit, "ARCHIVE_AUDIT_PAGE_SIZE", 1)
    key = selection.expected.fence.key
    with store.transaction() as db:
        first = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(first, ReviewGenerationArchiveScan)

    before = current(store, selection)
    actor = OperatorActor("operator", "test")
    assert isinstance(workflow.cancel_rearm(
        CancelRearm("cancel:after-audit-page", before.revision, "rearm:archive-audit",
                    "stop", expected_binding=before.binding), actor,
    ), WorkflowCommitted)
    stopped = current(store, selection)
    assert isinstance(workflow.continue_rearm(
        ContinueGrantedRearm("rearm:after-audit-page", stopped.revision,
                             "rearm:archive-audit", expected_binding=stopped.binding), actor,
    ), WorkflowCommitted)
    assert current(store, selection).revision.fence.generation == first.through_generation + 1

    with store.transaction() as db:
        complete = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(complete, CompletedReviewGenerationArchiveScan)
    assert complete.through_generation == first.through_generation
    assert complete.final_cursor is not None
    with store.transaction() as db:
        audited = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(audited, CompletedReviewGenerationArchiveAudit)
    assert audited.generation_scan == complete
    with store.transaction() as db:
        resumed = advance_review_generation_archive_audit(db, key, now=now)
    assert isinstance(resumed, ReviewGenerationArchiveScan)
    assert resumed.through_generation == complete.through_generation + 1
    assert resumed.next_generation == resumed.origin.rejection.fence.generation + 2


def test_generation_archive_scan_update_rolls_back_with_failed_cursor_write(rig, monkeypatch):
    store, selection, _ = _two_generations(rig)
    now = store.now()
    monkeypatch.setattr(archive_audit, "ARCHIVE_AUDIT_PAGE_SIZE", 1)
    with store.transaction() as db:
        first = advance_review_generation_archive_audit(db, selection.expected.fence.key, now=now)
    with store.transaction() as db:
        db.execute(
            "CREATE TRIGGER abort_archive_scan BEFORE UPDATE ON level_state "
            "WHEN NEW.namespace='review-generation-archive-scan' "
            "BEGIN SELECT RAISE(ABORT,'cursor write failed'); END",
        )
    with pytest.raises(sqlite3.IntegrityError, match="cursor write failed"):
        with store.transaction() as db:
            advance_review_generation_archive_audit(db, selection.expected.fence.key, now=now)
    assert _stored_state(store) == first


def test_generation_archive_scan_refuses_malformed_persisted_cursor(rig):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    now = store.now()
    with store.transaction() as db:
        advance_review_generation_archive_audit(db, key, now=now)
        db.execute(
            "UPDATE level_state SET payload_json='{}' "
            "WHERE namespace='review-generation-archive-scan'",
        )
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="cursor is malformed"):
            advance_review_generation_archive_audit(db, key, now=now)


def test_empty_generation_archive_scan_completes_without_source_reads(rig):
    store, snapshot, *_ = rig
    now = store.now()
    with store.transaction() as db:
        completed = advance_review_generation_archive_audit(db, snapshot.revision.fence.key, now=now)
    assert isinstance(completed, CompletedReviewGenerationArchiveAudit)
    assert completed.generation_scan.final_cursor is None
    assert completed.generation_scan.through_generation == completed.generation_scan.origin.rejection.fence.generation
    assert _stored_state(store) == completed
