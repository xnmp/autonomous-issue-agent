"""Completed archive passes must describe one current execution."""

from dataclasses import replace
from datetime import timedelta

import pytest

from issue_agent.adapters.review_checkpoint_archive_audit import (
    _audit_key as _checkpoint_audit_key, advance_review_checkpoint_archive_audit,
)
from issue_agent.adapters.review_execution_archive_audit import (
    advance_review_generation_archive_audit,
)
from issue_agent.adapters.review_execution_archive_evidence import (
    observe_initial_review_execution_archive_evidence,
    observe_review_execution_archive_evidence,
)
from issue_agent.adapters.review_execution_compaction import observe_review_checkpoint_manifest
from issue_agent.adapters.review_lifecycle_archive_audit import (
    advance_review_lifecycle_archive_audit,
)
from issue_agent.adapters.review_initial_checkpoint_absence import (
    advance_initial_checkpoint_absence_audit,
)
from issue_agent.adapters.review_archive_source_versions import read_review_archive_source_versions
from issue_agent.adapters.review_checkpoint_application_source import (
    REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, read_review_checkpoint_application_source,
)
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.review_round_archive_audit import (
    _current_execution, advance_review_round_archive_audit,
)
from issue_agent.adapters.review_round_receipt_archive_audit import (
    advance_review_round_receipt_archive_audit,
)
from issue_agent.adapters.workflow_rows import (
    REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, REVIEW_EXECUTION_ORIGIN_NAMESPACE,
    WorkflowAuthorityError, review_execution_checkpoint_receipt_id,
)
from issue_agent.domain.review_checkpoint_archive import (
    CompletedReviewCheckpointArchiveAudit, ReviewCheckpointArchiveCursor,
)
from issue_agent.domain.review_execution import (
    CompletedReviewGenerationArchiveAudit, ReviewGenerationArchiveCursor,
)
from issue_agent.domain.review_execution_archive_evidence import (
    ObservedInitialReviewExecutionArchiveEvidence,
    ObservedReviewExecutionArchiveEvidence,
)
from issue_agent.domain.review_lifecycle_archive_audit import CompletedReviewLifecycleArchiveAudit
from issue_agent.domain.review_initial_checkpoint_absence import (
    BlockedInitialCheckpointAbsence, CompletedInitialCheckpointAbsence,
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
from tests.adapters.test_review_archive_source_versions import _insert_lifecycle_command
from tests.adapters.test_review_productive_settlement import confirmed_copy, workflow_for


def _complete_source_passes(store, key, now, *, initial=False):
    from issue_agent.adapters.review_archive_identity import advance_review_archive_identity_audit
    from issue_agent.domain.review_archive_identity import CompletedReviewArchiveIdentities

    for _ in range(64):
        with store.transaction() as db:
            identities = advance_review_archive_identity_audit(db, now=now)
        if isinstance(identities, CompletedReviewArchiveIdentities):
            break
    else:
        raise AssertionError("archive identity audit did not complete")
    for advance, completed in (
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
    for _ in range(64):
        with store.transaction() as db:
            state = advance_review_lifecycle_archive_audit(db, key.repository, now=now)
        if isinstance(state, CompletedReviewLifecycleArchiveAudit):
            break
    else:
        raise AssertionError("lifecycle audit did not complete")
    if initial:
        with store.transaction() as db:
            binding = _current_execution(db, key).checkpoint.state.binding
        for _ in range(64):
            with store.transaction() as db:
                state = advance_initial_checkpoint_absence_audit(db, binding, now=now)
            if isinstance(state, CompletedInitialCheckpointAbsence):
                break
        else:
            raise AssertionError("initial checkpoint absence audit did not complete")


def _complete(store, key, now):
    _complete_source_passes(store, key, now)
    for _ in range(64):
        with store.transaction() as db:
            state = advance_review_checkpoint_archive_audit(db, key, now=now)
        if isinstance(state, CompletedReviewCheckpointArchiveAudit):
            break
    else:
        raise AssertionError("checkpoint audit did not complete")


def test_initial_archive_join_precedes_first_checkpoint_and_has_no_writes(active):
    store, snapshot = active
    key = snapshot.revision.fence.key
    now = store.now()
    _complete_source_passes(store, key, now, initial=True)
    with store.transaction() as db:
        before = db.total_changes
        observed = observe_initial_review_execution_archive_evidence(db, key, now=now)
        assert db.total_changes == before
    assert isinstance(observed, ObservedInitialReviewExecutionArchiveEvidence)
    assert observed.execution.checkpoint_receipt is None
    assert observed.rounds.through_sequence == 0
    assert observed.receipts.expected.through_sequence == 0


def test_initial_archive_join_covers_a_finalized_first_checkpoint_candidate(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    key = selection.expected.fence.key
    now = store.now()
    _complete_source_passes(store, key, now, initial=True)
    with store.transaction() as db:
        observed = observe_initial_review_execution_archive_evidence(db, key, now=now)
        manifest = observe_review_checkpoint_manifest(db, key, plan.round.sequence)
    assert observed.rounds.through_sequence == plan.round.sequence
    assert observed.receipts.expected.rolling_sha256 == manifest.receipt.round_stream_sha256
    assert observed.generations.generation_scan.final_cursor is None


def test_initial_archive_join_detects_an_unclaimed_checkpoint_receipt(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    key = selection.expected.fence.key
    now = store.now()
    _complete_source_passes(store, key, now, initial=True)
    with store.transaction() as db:
        manifest = observe_review_checkpoint_manifest(db, key, plan.round.sequence)
        receipt_id = review_execution_checkpoint_receipt_id(manifest.receipt)
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES (?,?,?,?,?)",
            (REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, key.repository,
             receipt_id, now.isoformat(), encode_workflow(manifest.receipt)),
        )
        with pytest.raises(WorkflowAuthorityError, match="archive .* census is stale"):
            observe_initial_review_execution_archive_evidence(db, key, now=now)
    binding = manifest.receipt.previous_checkpoint.state.binding
    for _ in range(64):
        with store.transaction() as db:
            state = advance_initial_checkpoint_absence_audit(db, binding, now=now)
        if isinstance(state, BlockedInitialCheckpointAbsence):
            break
    else:
        raise AssertionError("unclaimed checkpoint receipt was not found")
    assert "already has a checkpoint receipt" in state.reason
    with store.transaction() as db:
        db.execute(
            "DELETE FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, key.repository, receipt_id),
        )
    _complete_source_passes(store, key, now, initial=True)
    with store.transaction() as db:
        state = advance_initial_checkpoint_absence_audit(db, binding, now=now)
    assert isinstance(state, CompletedInitialCheckpointAbsence)
    with store.transaction() as db:
        restored = observe_initial_review_execution_archive_evidence(db, key, now=now)
    assert restored.checkpoint_absence == state
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-checkpoint-application',?,'orphan:after-repair',?, '{}')",
            (key.repository, now.isoformat()),
        )
        with pytest.raises(WorkflowAuthorityError, match="archive .* census is stale"):
            observe_initial_review_execution_archive_evidence(db, key, now=now)
    for _ in range(64):
        with store.transaction() as db:
            orphan = advance_initial_checkpoint_absence_audit(db, binding, now=now)
        if isinstance(orphan, BlockedInitialCheckpointAbsence):
            break
    else:
        raise AssertionError("orphan application did not block recensus")
    assert "unreadable" in orphan.reason


def test_initial_archive_join_rejects_a_compacted_execution(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="uncompacted"):
            observe_initial_review_execution_archive_evidence(db, key, now=store.now())


def test_initial_archive_join_rejects_a_prior_checkpoint_cursor(active):
    store, snapshot = active
    key = snapshot.revision.fence.key
    execution = snapshot.execution
    now = store.now()
    _complete_source_passes(store, key, now, initial=True)
    prior = CompletedReviewCheckpointArchiveAudit(
        "checkpoint:unexpected", execution.origin, "0" * 64,
        "0" * 64, 1, now, now + timedelta(seconds=3600), "0" * 64,
    )
    with store.transaction() as db:
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) "
            "VALUES (?,?,?,?,?)",
            ("review-checkpoint-archive-scan-v4", key.repository,
             _checkpoint_audit_key(key, execution.binding.execution_epoch,
                                   execution.binding.execution_episode),
             encode_workflow(prior), now.isoformat()),
        )
        with pytest.raises(WorkflowAuthorityError, match="unexpected checkpoint cursor"):
            observe_initial_review_execution_archive_evidence(db, key, now=now)


def test_initial_archive_join_rejects_mutation_after_completed_census(active):
    store, snapshot = active
    key = snapshot.revision.fence.key
    now = store.now()
    _complete_source_passes(store, key, now, initial=True)
    with store.transaction() as db:
        _insert_lifecycle_command(db, "foreign:initial", kind="other")
        with pytest.raises(WorkflowAuthorityError, match="lifecycle census is stale"):
            observe_initial_review_execution_archive_evidence(db, key, now=now)


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


def test_archive_join_requires_completed_lifecycle_census(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        db.execute("DELETE FROM level_state WHERE namespace='review-lifecycle-archive-scan-v1'")
        with pytest.raises(WorkflowAuthorityError, match="awaits complete"):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rejects_mutated_lifecycle_source_until_recensus(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        _insert_lifecycle_command(db, "foreign:after-census", kind="other")
        with pytest.raises(WorkflowAuthorityError, match="lifecycle census is stale"):
            observe_review_execution_archive_evidence(db, key, now=now)
    for _ in range(64):
        with store.transaction() as db:
            state = advance_review_lifecycle_archive_audit(db, key.repository, now=now)
        if isinstance(state, CompletedReviewLifecycleArchiveAudit):
            break
    else:
        raise AssertionError("changed lifecycle audit did not complete")
    with store.transaction() as db:
        observed = observe_review_execution_archive_evidence(db, key, now=now)
    assert observed.lifecycle.source_version == state.source_version


def test_archive_join_rejects_lifecycle_census_from_another_repository(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        observed = observe_review_execution_archive_evidence(db, key, now=now)
    with pytest.raises(ValueError, match="another repository"):
        replace(observed, lifecycle=replace(observed.lifecycle, repository="other/repo"))


def test_archive_join_rejects_overdue_lifecycle_census(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        observed = observe_review_execution_archive_evidence(db, key, now=now)
    due = now + timedelta(seconds=1)
    with pytest.raises(ValueError, match="premature or overdue"):
        replace(
            observed,
            lifecycle=replace(observed.lifecycle, next_due_at=due),
            observed_at=due,
        )


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


def test_archive_join_rejects_foreign_generation_source_after_completed_pass(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        before = read_review_archive_source_versions(db, key.repository)
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-execution-generation','other/repo','foreign:receipt',"
            "'2026-09-29T00:00:00+00:00','{}')",
        )
        after = read_review_archive_source_versions(db, key.repository)
        assert (after.round_rows, after.all_round_rows, after.round_receipts) == (
            before.round_rows, before.all_round_rows, before.round_receipts,
        )
        assert after.generation_sources == before.generation_sources + 1
        with pytest.raises(WorkflowAuthorityError, match="generation census is stale"):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rejects_unversioned_generation_completion(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM level_state "
            "WHERE namespace='review-generation-archive-scan'",
        ).fetchone()
        completed = decode_workflow(row["payload_json"], ReviewGenerationArchiveCursor).state
        db.execute(
            "UPDATE level_state SET payload_json=? "
            "WHERE namespace='review-generation-archive-scan'",
            (encode_workflow(completed),),
        )
        with pytest.raises(WorkflowAuthorityError, match="generation census is stale"):
            observe_review_execution_archive_evidence(db, key, now=now)


@pytest.mark.parametrize("namespace", (
    "review-execution-origin", "review-execution-checkpoint", "review-checkpoint-application",
))
def test_archive_join_rejects_foreign_checkpoint_source_after_completed_pass(active, namespace):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        before = read_review_archive_source_versions(db, key.repository)
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES (?,'other/repo','foreign:receipt','2026-09-29T00:00:00+00:00','{}')",
            (namespace,),
        )
        after = read_review_archive_source_versions(db, key.repository)
        assert after.checkpoint_sources == before.checkpoint_sources + 1
        with pytest.raises(WorkflowAuthorityError, match="archive .* census is stale"):
            observe_review_execution_archive_evidence(db, key, now=now)


@pytest.mark.parametrize("table,column", (
    ("workflows", "episode_json"),
    ("workflow_components", "episode_id"),
))
def test_archive_join_rejects_workflow_source_after_completed_pass(active, table, column):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        before = read_review_archive_source_versions(db, key.repository)
        changed = db.execute(
            f"UPDATE {table} SET {column}={column} WHERE repository=? AND target_kind='pr'",
            (key.repository,),
        )
        assert changed.rowcount > 0
        after = read_review_archive_source_versions(db, key.repository)
        assert after.workflow_rows == before.workflow_rows + changed.rowcount
        with pytest.raises(WorkflowAuthorityError, match="checkpoint census is stale"):
            observe_review_execution_archive_evidence(db, key, now=now)


def test_archive_join_rejects_unversioned_checkpoint_completion(active):
    store, key, _, _ = _two_valid_checkpoints(active)
    now = store.now()
    _complete(store, key, now)
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM level_state "
            "WHERE namespace='review-checkpoint-archive-scan-v4'",
        ).fetchone()
        completed = decode_workflow(row["payload_json"], ReviewCheckpointArchiveCursor).state
        db.execute(
            "UPDATE level_state SET payload_json=? "
            "WHERE namespace='review-checkpoint-archive-scan-v4'",
            (encode_workflow(completed),),
        )
        with pytest.raises(WorkflowAuthorityError, match="checkpoint census is stale"):
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
        with pytest.raises(WorkflowAuthorityError, match="archive .* census is stale"):
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
        with pytest.raises(WorkflowAuthorityError, match="archive .* census is stale"):
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
