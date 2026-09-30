"""Archived checkpoint admission follows a complete, durable source census."""

from dataclasses import replace
from datetime import timedelta

import pytest

from issue_agent.adapters.review_execution_archive_admission_audit import (
    advance_review_execution_archive_admission_audit,
)
from issue_agent.adapters.review_execution_archive_gate import (
    read_review_execution_archive_gate, require_review_execution_archive_admission,
)
from issue_agent.adapters.review_execution_checkpoint_commit import commit_review_execution_checkpoint
from issue_agent.adapters.effects import bound_review_command_id
from issue_agent.adapters.control_store import AccountingDisposition
from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row
from issue_agent.domain.review_execution_archive_gate import (
    BlockedReviewExecutionArchive, CertifiedReviewExecutionArchive,
)
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.contracts import CommandKind, EffectReceipt, EffectStatus, LifecycleCommand
from issue_agent.domain.review_rounds import ReviewSubmissionKind
from tests.adapters.test_review_execution_checkpoint_commit import _copy_source, _paid_source
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig
from tests.adapters.test_review_execution_archive_evidence import _complete
from tests.adapters.test_review_execution_admission import request
from tests.adapters.test_review_execution_launch import register, launch_receipt
from tests.adapters.test_review_execution_return import returned, settlement
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_productive_settlement import workflow_for


def _published_copy(rig):
    store, key, plan, now = _copy_source(rig)
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        committed = commit_review_execution_checkpoint(
            db, key, snapshot.revision, plan.round.sequence, now=now,
        )
    return store, key, plan, now, snapshot.revision, committed


def _next_selection(rig, store, *, observed_at):
    _, _, gateway, selection, _ = rig
    assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name,
                                "o/r:review:test", frozenset())
    occurrence = store.active_dispatch_claims("o/r", "review")[0].claim_key
    gateway.view = replace(gateway.view, head_ref_oid="d" * 40)
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, selection.expected.fence.key)
        assert snapshot is not None
    return replace(
        selection, request_id="preflight:after-checkpoint", expected=snapshot.revision,
        binding=snapshot.execution.binding, claim_occurrence_id=occurrence,
        observation=replace(selection.observation, head_sha="d" * 40,
                            observed_at=observed_at),
    )


def test_completed_census_renews_admission_after_paged_progress(rig):
    store, key, _, now, _, _ = _published_copy(rig)
    audit_at = now + timedelta(minutes=1)
    observations = []
    for _ in range(64):
        with store.transaction() as db:
            gate = advance_review_execution_archive_admission_audit(db, key, now=audit_at)
        observations.append(gate)
        if isinstance(gate, CertifiedReviewExecutionArchive) and gate.certified_at == audit_at:
            break
    else:
        raise AssertionError("archived checkpoint census did not complete")
    assert len(observations) > 1
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        assert read_review_execution_archive_gate(db, key, snapshot.execution) == gate
        require_review_execution_archive_admission(db, key, snapshot.execution, now=audit_at)


def test_overdue_archive_can_recertify_after_complete_fresh_pass(rig):
    store, key, _, _, _, _ = _published_copy(rig)
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        prior = read_review_execution_archive_gate(db, key, snapshot.execution)
        assert isinstance(prior, CertifiedReviewExecutionArchive)
    audit_at = prior.next_due_at
    for _ in range(64):
        with store.transaction() as db:
            gate = advance_review_execution_archive_admission_audit(db, key, now=audit_at)
        if isinstance(gate, CertifiedReviewExecutionArchive) and gate.certified_at == audit_at:
            break
    else:
        raise AssertionError("overdue archive never recertified")
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        require_review_execution_archive_admission(db, key, snapshot.execution, now=audit_at)


def test_expiry_between_reservation_and_launch_releases_unlaunched_money(rig, monkeypatch):
    store, key, _, now, _, _ = _published_copy(rig)
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        gate = read_review_execution_archive_gate(db, key, snapshot.execution)
        assert isinstance(gate, CertifiedReviewExecutionArchive)
    selected_at = now + timedelta(minutes=1)
    monkeypatch.setattr(store, "_now", lambda: selected_at)
    selection = _next_selection(rig, store, observed_at=selected_at)
    plan = rig[4].preflight(selection)
    planned = stage(plan)
    rig[4].register_stage(plan.round.round_id, current(store, selection).revision, planned)
    proposed = request(planned.children[0])
    assert store.reserve(proposed).admitted
    reserved = store.snapshot(proposed.scope).reserved.micros
    monkeypatch.setattr(store, "_now", lambda: gate.next_due_at)
    with pytest.raises(WorkflowAuthorityError, match="overdue"):
        register(store, proposed)
    assert store.leases() == () and launch_receipt(store) is None
    released = store.release_unlaunched(proposed.run_id, "archive overdue")
    assert released.disposition is AccountingDisposition.AVAILABLE
    assert store.snapshot(proposed.scope).reserved.micros < reserved


def test_overdue_certificate_refuses_admission_but_exact_publication_retry_is_inert(rig):
    store, key, plan, now, expected, committed = _published_copy(rig)
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        gate = read_review_execution_archive_gate(db, key, snapshot.execution)
        assert isinstance(gate, CertifiedReviewExecutionArchive)
        with pytest.raises(WorkflowAuthorityError, match="overdue"):
            require_review_execution_archive_admission(
                db, key, snapshot.execution, now=gate.next_due_at,
            )
    baseline = dump(store)
    with store.transaction() as db:
        retry = commit_review_execution_checkpoint(
            db, key, expected, plan.round.sequence, now=gate.next_due_at,
        )
    assert retry.idempotent and retry.receipt_id == committed.receipt_id
    assert dump(store) == baseline


def test_overdue_certificate_blocks_new_public_preflight_but_replays_old_one(rig, monkeypatch):
    store, key, plan, _, _, _ = _published_copy(rig)
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        gate = read_review_execution_archive_gate(db, key, snapshot.execution)
        assert isinstance(gate, CertifiedReviewExecutionArchive)
    monkeypatch.setattr(store, "_now", lambda: gate.next_due_at)
    fresh = _next_selection(rig, store, observed_at=gate.next_due_at)
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="awaits archive maintenance"):
        rig[4].preflight(fresh)
    after_maintenance = dump(store)
    assert after_maintenance != before
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after == snapshot
        assert read_review_execution_archive_gate(db, key, after.execution) == gate
        assert db.execute("SELECT COUNT(*) FROM review_execution_rounds").fetchone()[0] == 1
    assert rig[4].preflight(rig[3]) == plan
    assert dump(store) == after_maintenance


def test_overdue_certificate_allows_parent_loss_recovery_of_existing_round(rig, monkeypatch):
    store, key, _, now, _, _ = _published_copy(rig)
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        gate = read_review_execution_archive_gate(db, key, snapshot.execution)
        assert isinstance(gate, CertifiedReviewExecutionArchive)
    selected_at = now + timedelta(minutes=1)
    monkeypatch.setattr(store, "_now", lambda: selected_at)
    selection = _next_selection(rig, store, observed_at=selected_at)
    pending = rig[4].preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, "o/r:review:test")
    monkeypatch.setattr(store, "_now", lambda: gate.next_due_at)
    rig[4].seal_abandoned_planning(pending.round.round_id)
    recovered = rig[4].finalize_abandoned_round(pending.round.round_id)
    assert recovered.outcome.round == pending.round
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert after.execution.last_sequence == pending.round.sequence


def test_overdue_certificate_allows_existing_paid_submission_settlement(rig, monkeypatch):
    store, key, _, now, _, _ = _published_copy(rig)
    gateway = rig[2]
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        gate = read_review_execution_archive_gate(db, key, snapshot.execution)
        assert isinstance(gate, CertifiedReviewExecutionArchive)
    selected_at = now + timedelta(minutes=1)
    monkeypatch.setattr(store, "_now", lambda: selected_at)
    selection = _next_selection(rig, store, observed_at=selected_at)
    pending = rig[4].preflight(selection)
    planned = stage(pending)
    rig[4].register_stage(pending.round.round_id, current(store, selection).revision, planned)
    proposed = request(planned.children[0])
    assert store.reserve(proposed).admitted
    register(store, proposed)
    store.settle_review_return(settlement(proposed), returned(store, proposed))
    binding = current(store, selection).binding
    operation = BoundReviewSubmissionOp(
        9, "APPROVE", "approved", "d" * 40, "review-bot", "0" * 64, None,
    )
    command_id = bound_review_command_id(operation, binding)
    command = LifecycleCommand(
        command_id, f"github:{command_id}", CommandKind.SUBMIT_REVIEW,
        "pr:9", operation.head, operation.payload(), "test", "o/r", OWNER,
        workflow=binding,
    )
    rig[4].register_submission(pending.round.round_id, current(store, selection).revision,
                               command, ReviewSubmissionKind.PAID)
    rig[4].seal_completed_planning(pending.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, selected_at, selected_at,
                                      EffectStatus.SUCCEEDED, remote_object_id="review:late"))
    monkeypatch.setattr(store, "_now", lambda: gate.next_due_at)
    workflow_for(store, gateway).settle_submission(command.command_id)
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert after.execution.state.productive_sequence == pending.round.sequence


def test_second_checkpoint_preserves_older_exact_retry(rig, monkeypatch):
    store, key, first, now, first_expected, first_commit = _published_copy(rig)
    later = now + timedelta(minutes=1)
    monkeypatch.setattr(store, "_now", lambda: later)
    selection = _next_selection(rig, store, observed_at=later)
    second = rig[4].preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, "o/r:review:test")
    rig[4].seal_abandoned_planning(second.round.round_id)
    rig[4].finalize_abandoned_round(second.round.round_id)
    _complete(store, key, later)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
        second_commit = commit_review_execution_checkpoint(
            db, key, before.revision, second.round.sequence, now=later,
        )
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert after.execution.checkpoint.through_sequence == second.round.sequence
        assert after.execution.checkpoint_receipt == second_commit.receipt_id
        assert after.execution.state == before.execution.state
    committed = dump(store)
    with store.transaction() as db:
        retry = commit_review_execution_checkpoint(
            db, key, first_expected, first.round.sequence, now=later,
        )
    assert retry.idempotent and retry.receipt_id == first_commit.receipt_id
    assert dump(store) == committed


def test_source_discrepancy_blocks_admission_and_preserves_checkpoint_authority(rig):
    store, key, _, now, _, committed = _published_copy(rig)
    with store.transaction() as db:
        row = db.execute("SELECT outcome_json FROM review_execution_rounds").fetchone()
        assert row is not None
        original_outcome = row[0]
        db.execute("UPDATE review_execution_rounds SET outcome_json='{}'")
    with store.transaction() as db:
        gate = advance_review_execution_archive_admission_audit(
            db, key, now=now + timedelta(minutes=1),
        )
    assert isinstance(gate, BlockedReviewExecutionArchive)
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError):
            load_review_workflow_row(db, key)
        db.execute("UPDATE review_execution_rounds SET outcome_json=?", (original_outcome,))
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        assert snapshot.execution.checkpoint_receipt == committed.receipt_id
        with pytest.raises(WorkflowAuthorityError, match="discrepancy"):
            require_review_execution_archive_admission(
                db, key, snapshot.execution, now=now + timedelta(minutes=1),
            )
    for _ in range(64):
        with store.transaction() as db:
            gate = advance_review_execution_archive_admission_audit(
                db, key, now=now + timedelta(minutes=2),
            )
        if isinstance(gate, CertifiedReviewExecutionArchive):
            break
    else:
        raise AssertionError("repaired sources did not complete a fresh census")
    assert gate.certified_at == now + timedelta(minutes=2)
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        require_review_execution_archive_admission(
            db, key, snapshot.execution, now=gate.certified_at,
        )


def test_new_lease_on_retired_paid_run_blocks_a_fresh_completed_certificate(rig):
    store, key, plan, now = _paid_source(rig)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
        committed = commit_review_execution_checkpoint(
            db, key, before.revision, plan.round.sequence, now=now,
        )
    audit_at = now + timedelta(minutes=1)
    for _ in range(128):
        with store.transaction() as db:
            gate = advance_review_execution_archive_admission_audit(db, key, now=audit_at)
        if isinstance(gate, CertifiedReviewExecutionArchive) and gate.certified_at == audit_at:
            break
    else:
        raise AssertionError("paid checkpoint census did not complete")

    with store.transaction() as db:
        run = db.execute(
            "SELECT run_id,dispatch_id,owner_id FROM runs WHERE review_round_id=?",
            (plan.round.round_id,),
        ).fetchone()
        assert run is not None
        db.execute(
            "INSERT INTO leases(run_id,dispatch_id,owner_id,pid,pgid,started_at) "
            "VALUES (?,?,?,1,1,?)",
            (*run, audit_at.isoformat()),
        )

    later = audit_at + timedelta(minutes=1)
    for _ in range(128):
        with store.transaction() as db:
            gate = advance_review_execution_archive_admission_audit(db, key, now=later)
        if isinstance(gate, BlockedReviewExecutionArchive):
            break
    else:
        raise AssertionError("a retained live lease did not invalidate the completed proof")
    assert "lease" in gate.reason
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        assert snapshot.execution.checkpoint_receipt == committed.receipt_id
        with pytest.raises(WorkflowAuthorityError, match="discrepancy"):
            require_review_execution_archive_admission(db, key, snapshot.execution, now=later)
        db.execute("DELETE FROM leases WHERE run_id=?", (run["run_id"],))

    repaired_at = later + timedelta(minutes=1)
    for _ in range(128):
        with store.transaction() as db:
            gate = advance_review_execution_archive_admission_audit(db, key, now=repaired_at)
        if isinstance(gate, CertifiedReviewExecutionArchive) and gate.certified_at == repaired_at:
            break
    else:
        raise AssertionError("lease removal did not permit fresh recertification")
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        require_review_execution_archive_admission(db, key, snapshot.execution, now=repaired_at)
