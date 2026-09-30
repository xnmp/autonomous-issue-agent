"""Checkpoint publication uses current archived SQL proof and is atomic."""

import sqlite3
from datetime import timedelta

import pytest

import issue_agent.adapters.review_execution_checkpoint_commit as checkpoint_writer
from issue_agent.adapters.review_execution_checkpoint_commit import commit_review_execution_checkpoint
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.review_rounds import ReviewSubmissionKind
from tests.adapters.test_review_execution_admission import request
from tests.adapters.test_review_execution_archive_evidence import _complete_source_passes
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig
from tests.adapters.test_review_execution_launch import register
from tests.adapters.test_review_execution_return import returned, settlement
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_execution_submission import command_for
from tests.adapters.test_review_productive_settlement import confirmed_copy, workflow_for


def _copy_source(rig):
    store, gateway, selection, _, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).settle_submission(command.command_id)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    key = selection.expected.fence.key
    now = store.now()
    _complete_source_passes(store, key, now, initial=True)
    return store, key, plan, now


def _paid_source(rig):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    planned = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, planned)
    proposed = request(planned.children[0])
    assert store.reserve(proposed).admitted
    register(store, proposed)
    store.settle_review_return(settlement(proposed), returned(store, proposed))
    command = command_for(current(store, selection), patch_id="0" * 64)
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.PAID)
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:paid"))
    workflow_for(store, gateway).record_review(command.command_id)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    key = selection.expected.fence.key
    now = store.now()
    _complete_source_passes(store, key, now, initial=True)
    return store, key, plan, now


def _abandoned_source(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    control.finalize_abandoned_round(plan.round.round_id)
    key = selection.expected.fence.key
    now = store.now()
    _complete_source_passes(store, key, now, initial=True)
    return store, key, plan, now


def test_copy_checkpoint_commits_receipts_application_publication_and_gate(rig):
    store, key, plan, now = _copy_source(rig)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
        result = commit_review_execution_checkpoint(db, key, before.revision,
                                                    plan.round.sequence, now=now)
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert after.execution.state == before.execution.state
        assert after.execution.checkpoint.through_sequence == plan.round.sequence
        assert after.execution.checkpoint_receipt == result.receipt_id
        assert after.revision.version == before.revision.version + 1
        assert not result.idempotent
        namespaces = {
            row[0] for row in db.execute(
                "SELECT namespace FROM dedupe_keys WHERE key=?", (result.receipt_id,),
            )
        }
        assert "review-execution-checkpoint" in namespaces
        assert "review-checkpoint-application" in namespaces
        assert db.execute(
            "SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-checkpoint-publication'",
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM level_state WHERE namespace='review-execution-archive-gate-v1'",
        ).fetchone()[0] == 1
    committed = dump(store)
    with store.transaction() as db:
        retry = commit_review_execution_checkpoint(db, key, before.revision,
                                                   plan.round.sequence, now=now)
        assert retry.idempotent and retry.receipt_id == result.receipt_id
    assert dump(store) == committed


@pytest.mark.parametrize("source", [_paid_source, _abandoned_source])
def test_checkpoint_retires_real_paid_and_abandoned_rounds_without_changing_state(rig, source):
    store, key, plan, now = source(rig)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
        committed = commit_review_execution_checkpoint(
            db, key, before.revision, plan.round.sequence, now=now,
        )
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert after.execution.state == before.execution.state
        assert after.execution.checkpoint.through_sequence == plan.round.sequence
        assert after.execution.checkpoint_receipt == committed.receipt_id
        assert after.execution.boundaries == ()


def test_changed_source_version_refuses_without_writes(rig):
    store, key, plan, now = _copy_source(rig)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
        db.execute("UPDATE review_execution_rounds SET plan_json=plan_json")
    changed = dump(store)
    with pytest.raises(WorkflowAuthorityError):
        with store.transaction() as db:
            commit_review_execution_checkpoint(db, key, before.revision,
                                               plan.round.sequence, now=now)
    assert dump(store) == changed


def test_corrupt_retained_round_refuses_without_publishing_receipts(rig):
    store, key, plan, now = _copy_source(rig)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
        db.execute("UPDATE review_execution_rounds SET outcome_json='{}'")
    corrupted = dump(store)
    with pytest.raises(WorkflowAuthorityError):
        with store.transaction() as db:
            commit_review_execution_checkpoint(db, key, before.revision,
                                               plan.round.sequence, now=now)
    assert dump(store) == corrupted


def test_expired_source_passes_refuse_without_writes(rig):
    store, key, plan, now = _copy_source(rig)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
    original = dump(store)
    with pytest.raises(WorkflowAuthorityError):
        with store.transaction() as db:
            commit_review_execution_checkpoint(db, key, before.revision,
                                               plan.round.sequence, now=now + timedelta(days=1))
    assert dump(store) == original


def test_late_gate_write_fault_rolls_back_all_checkpoint_publication(rig, monkeypatch):
    store, key, plan, now = _copy_source(rig)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
    original = dump(store)

    def fail_gate(*args, **kwargs):
        raise sqlite3.IntegrityError("injected gate write failure")

    monkeypatch.setattr(checkpoint_writer, "persist_review_execution_archive_gate", fail_gate)
    with pytest.raises(WorkflowAuthorityError, match="publication failed"):
        with store.transaction() as db:
            commit_review_execution_checkpoint(db, key, before.revision,
                                               plan.round.sequence, now=now)
    assert dump(store) == original


def test_caught_publication_fault_cannot_commit_partial_authority(rig, monkeypatch):
    store, key, plan, now = _copy_source(rig)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
    original = dump(store)

    def fail_gate(*args, **kwargs):
        raise sqlite3.IntegrityError("injected gate write failure")

    monkeypatch.setattr(checkpoint_writer, "persist_review_execution_archive_gate", fail_gate)
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="publication failed"):
            commit_review_execution_checkpoint(db, key, before.revision,
                                               plan.round.sequence, now=now)
        assert load_review_workflow_row(db, key) == before
    assert dump(store) == original


@pytest.mark.parametrize("mutation", (
    "claimed_at='broken'", "claimed_at='2026-09-29T01:00:00+00:00'",
    "reusable_after='2099-01-01'", "claimed_at=CAST(claimed_at AS BLOB)",
))
def test_retry_refuses_corrupt_publication_source(rig, mutation):
    store, key, plan, now = _copy_source(rig)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
        commit_review_execution_checkpoint(db, key, before.revision, plan.round.sequence, now=now)
        db.execute(f"UPDATE dedupe_keys SET {mutation} WHERE namespace='review-checkpoint-publication'")
    corrupted = dump(store)
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError):
            commit_review_execution_checkpoint(db, key, before.revision, plan.round.sequence, now=now)
    assert dump(store) == corrupted
