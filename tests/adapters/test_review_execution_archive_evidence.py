"""Completed archive passes must describe one current execution."""

from dataclasses import replace
from datetime import timedelta

import pytest

from issue_agent.adapters.review_checkpoint_archive_audit import (
    advance_review_checkpoint_archive_audit,
)
from issue_agent.adapters.review_execution_archive_audit import (
    advance_review_generation_archive_audit,
)
from issue_agent.adapters.review_execution_archive_evidence import (
    observe_review_execution_archive_evidence,
)
from issue_agent.adapters.review_archive_source_versions import read_review_archive_source_versions
from issue_agent.adapters.review_checkpoint_application_source import (
    REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, read_review_checkpoint_application_source,
)
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.review_round_archive_audit import advance_review_round_archive_audit
from issue_agent.adapters.review_round_receipt_archive_audit import (
    advance_review_round_receipt_archive_audit,
)
from issue_agent.adapters.workflow_rows import (
    REVIEW_EXECUTION_ORIGIN_NAMESPACE, WorkflowAuthorityError,
)
from issue_agent.domain.review_checkpoint_archive import CompletedReviewCheckpointArchiveAudit
from issue_agent.domain.review_execution import CompletedReviewGenerationArchiveAudit
from issue_agent.domain.review_execution_archive_evidence import (
    ObservedReviewExecutionArchiveEvidence,
)
from issue_agent.domain.review_round_archive import (
    CompletedReviewRoundArchiveScan, ReviewRoundArchiveCursor,
)
from issue_agent.domain.review_round_receipt_archive import CompletedReviewRoundReceiptArchiveAudit
from tests.adapters.test_review_checkpoint_archive_audit import (
    _generation_checkpoint, _real_selected_checkpoint, _two_valid_checkpoints,
)
from tests.adapters.test_review_execution_control import rig
from tests.adapters.test_review_execution_persistence import active


def _complete(store, key, now):
    for advance, completed in (
        (advance_review_checkpoint_archive_audit, CompletedReviewCheckpointArchiveAudit),
        (advance_review_round_archive_audit, CompletedReviewRoundArchiveScan),
        (advance_review_round_receipt_archive_audit, CompletedReviewRoundReceiptArchiveAudit),
        (advance_review_generation_archive_audit, CompletedReviewGenerationArchiveAudit),
    ):
        for _ in range(64):
            with store.transaction() as db:
                state = advance(db, key, now=now)
            if isinstance(state, completed):
                break
        else:
            raise AssertionError(f"{advance.__name__} did not complete")


def test_current_completed_archive_passes_join_without_writes(active):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        before = db.total_changes
        observed = observe_review_execution_archive_evidence(db, key, now=now)
        tip_source = read_review_checkpoint_application_source(db, key.repository, tip)
        assert db.total_changes == before
    assert observed.checkpoints.tip_receipt_id == tip
    assert observed.checkpoints.tip_application_source_sha256 == tip_source.source_sha256
    assert observed.rounds.through_sequence == 2
    assert observed.receipts.expected.through_sequence == 2
    assert observed.generations.generation_scan.final_cursor is None


def test_retired_generation_checkpoint_joins_its_final_source(rig):
    store, key, _, manifest = _generation_checkpoint(rig)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        observed = observe_review_execution_archive_evidence(db, key, now=now)
    assert observed.generations.generation_scan.final_cursor == manifest.plan.checkpoint.boundary_cursor


def test_selected_tail_checkpoint_joins_remaining_generation(rig):
    store, key, manifest = _real_selected_checkpoint(rig)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        observed = observe_review_execution_archive_evidence(db, key, now=now)
    assert observed.execution.boundaries == manifest.plan.remaining_boundaries
    assert observed.generations.generation_scan.final_cursor is not None


def test_archive_join_requires_all_completed_passes(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="awaits complete"):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rejects_overdue_and_future_completion(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        observed = observe_review_execution_archive_evidence(db, key, now=now)
        with pytest.raises(WorkflowAuthorityError, match="disagree"):
            observe_review_execution_archive_evidence(
                db, key, now=observed.checkpoints.next_due_at,
            )
    with pytest.raises(ValueError, match="premature or overdue"):
        replace(observed, observed_at=now - timedelta(seconds=1))


def test_archive_join_rejects_mismatched_completed_frontier(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        observed = observe_review_execution_archive_evidence(db, key, now=now)
    with pytest.raises(ValueError, match="source frontiers"):
        replace(observed, rounds=replace(observed.rounds, through_sequence=3))
    assert isinstance(observed, ObservedReviewExecutionArchiveEvidence)


def test_archive_join_rechecks_current_origin_source(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        observed = observe_review_execution_archive_evidence(db, key, now=now)
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-01-01T00:00:00+00:00' "
            "WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository,
             observed.execution.origin.evidence_id),
        )
        with pytest.raises(WorkflowAuthorityError):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rejects_round_mutation_after_completed_pass(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        changed = db.execute(
            "UPDATE review_execution_rounds SET planning_json=planning_json "
            "WHERE repository=?",
            (key.repository,),
        )
        assert changed.rowcount > 0
        with pytest.raises(WorkflowAuthorityError, match="round-row census is stale"):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rejects_unversioned_round_completion(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM level_state "
            "WHERE namespace='review-round-archive-scan'",
        ).fetchone()
        completed = decode_workflow(row["payload_json"], ReviewRoundArchiveCursor).state
        db.execute(
            "UPDATE level_state SET payload_json=? "
            "WHERE namespace='review-round-archive-scan'",
            (encode_workflow(completed),),
        )
        with pytest.raises(WorkflowAuthorityError, match="round-row census is stale"):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rejects_receipt_mutation_after_completed_pass(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        changed = db.execute(
            "UPDATE dedupe_keys SET payload_json=payload_json "
            "WHERE namespace='review-round-preflight' AND scope=?",
            (key.repository,),
        )
        assert changed.rowcount > 0
        with pytest.raises(WorkflowAuthorityError, match="receipt census is stale"):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rejects_foreign_round_mutation_after_receipt_pass(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
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
        assert after.round_rows == before.round_rows
        assert after.round_receipts == before.round_receipts
        assert after.all_round_rows == before.all_round_rows + 1
        with pytest.raises(WorkflowAuthorityError, match="receipt census is stale"):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rejects_receipt_pass_after_round_rescan(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        changed = db.execute(
            "UPDATE review_execution_rounds SET planning_json=planning_json "
            "WHERE repository=?",
            (key.repository,),
        )
        assert changed.rowcount > 0
    for _ in range(64):
        with store.transaction() as db:
            round_state = advance_review_round_archive_audit(db, key, now=now)
        if isinstance(round_state, CompletedReviewRoundArchiveScan):
            break
    else:
        raise AssertionError("round census did not complete its fresh pass")
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="receipt census is stale"):
            observe_review_execution_archive_evidence(db, key, now=now)


@pytest.mark.parametrize("damage", ("missing", "malformed"))
def test_archive_join_rechecks_current_checkpoint_application(active, damage):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        if damage == "missing":
            db.execute(
                "DELETE FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, key.repository, tip),
            )
        else:
            db.execute(
                "UPDATE dedupe_keys SET payload_json='{}' "
                "WHERE namespace=? AND scope=? AND key=?",
                (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, key.repository, tip),
            )
        with pytest.raises(WorkflowAuthorityError, match="checkpoint application"):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rejects_valid_application_change_after_completed_pass(active):
    store, key, _, tip = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        before = read_review_checkpoint_application_source(db, key.repository, tip)
        changed = replace(
            before.recorded, tail_start_sequence=before.checkpoint.checkpoint.through_sequence,
        )
        assert changed != before.recorded
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? "
            "WHERE namespace=? AND scope=? AND key=?",
            (encode_workflow(changed), REVIEW_CHECKPOINT_APPLICATION_NAMESPACE,
             key.repository, tip),
        )
        after = read_review_checkpoint_application_source(db, key.repository, tip)
        assert after.source_sha256 != before.source_sha256
        with pytest.raises(WorkflowAuthorityError, match="application changed"):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rechecks_current_following_generation_source(rig):
    store, key, manifest = _real_selected_checkpoint(rig)
    assert manifest.plan.remaining_boundaries
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        source = manifest.application.tail.source
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-01-01T00:00:00+00:00' "
            "WHERE namespace=? AND scope=? AND key=?",
            (source.accepted_namespace, key.repository, source.accepted_key),
        )
        with pytest.raises((WorkflowAuthorityError, ValueError)):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rejects_naive_time(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    with store.transaction() as db:
        with pytest.raises(ValueError, match="aware time"):
            observe_review_execution_archive_evidence(
                db, key, now=store.now().replace(tzinfo=None),
            )
