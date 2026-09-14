from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.adapters.control_store import ControlStoreError, SQLiteControlStore
from issue_agent.adapters.effects import command_for_action
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.domain.actions import PostComment, Target
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.workflow import ActiveReconciliation, ReconciliationOnly
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot


def fixture(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    state = resolve_snapshot()
    install_workflow(store, state)
    command = command_for_action(
        PostComment(Target.ISSUE, 1, "status"), repository="o/r",
        executor_identity="o/r:fix:test", workflow=state.binding,
    )
    return store, state, command


def test_restart_preserves_command_binding_and_old_generation_cannot_begin(tmp_path):
    store, state, command = fixture(tmp_path)
    store.enqueue_command(command)
    restarted = SQLiteControlStore(store.path)
    restarted.initialize()
    assert restarted.pending_commands()[0].command.workflow == state.binding
    with store.transaction() as db:
        db.execute("UPDATE workflows SET generation=generation+1, version=version+1")
    with pytest.raises(ControlStoreError, match="stale"):
        restarted.begin_command(command.command_id)
    pending = restarted.pending_commands()[0]
    assert pending.state == "pending"
    assert pending.attempts == 0


def test_generation_is_part_of_logical_command_identity(tmp_path):
    _store, state, old = fixture(tmp_path)
    new = command_for_action(
        PostComment(Target.ISSUE, 1, "status"), repository="o/r",
        executor_identity="o/r:fix:test",
        workflow=replace(state.binding, fence=replace(state.binding.fence, generation=3)),
    )
    assert new.command_id != old.command_id
    assert new.idempotency_key != old.idempotency_key


@pytest.mark.parametrize("invalid", ["target", "owner", "missing_binding"])
def test_enqueue_cannot_launder_wrong_workflow_authority(tmp_path, invalid):
    store, _state, command = fixture(tmp_path)
    if invalid == "target":
        command = replace(command, target="issue:2")
    elif invalid == "owner":
        command = replace(command, executor_identity="o/r:review:test")
    else:
        command = replace(command, workflow=None)
    with pytest.raises(ControlStoreError):
        store.enqueue_command(command)
    assert store.pending_commands() == ()


def test_reconciliation_only_command_cannot_bypass_expiry_through_legacy_helpers(tmp_path):
    store, _state, command = fixture(tmp_path)
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    with store.transaction() as db:
        db.execute(
            "UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
            (encode_workflow(ReconciliationOnly(ActiveReconciliation(now, now, 20), "uncertain")), str(command.command_id)),
        )
    with pytest.raises(ControlStoreError, match="reconciliation-only"):
        store.retry_reconciled_command(command.command_id)
    with store.transaction() as db:
        db.execute("UPDATE lifecycle_commands SET state='retryable_failure' WHERE command_id=?", (str(command.command_id),))
    with pytest.raises(ControlStoreError, match="reconciliation-only"):
        store.begin_command(command.command_id)
    assert store.pending_commands()[0].attempts == 1


def test_retry_archives_prior_receipt_before_replacing_current_outcome(tmp_path):
    store, _state, command = fixture(tmp_path)
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    receipt = EffectReceipt(
        command.command_id, command.executor_identity, now, now,
        EffectStatus.RETRYABLE_FAILURE, error="remote refusal",
    )
    store.record_effect(receipt)
    store.begin_command(command.command_id)

    assert store.effect_receipt(command.command_id) is None
    with store._connect() as db:
        evidence = db.execute(
            "SELECT kind, payload_json FROM workflow_evidence WHERE command_id=?",
            (str(command.command_id),),
        ).fetchall()
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "prior_receipt"
    assert "remote refusal" in evidence[0]["payload_json"]
    assert store.pending_commands()[0].attempts == 2


@pytest.mark.parametrize("resolution", ["verified_success", "exhausted"])
def test_reconciliation_and_exhaustion_preserve_replaced_receipt(tmp_path, resolution):
    store, _state, command = fixture(tmp_path)
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    initial = EffectStatus.UNKNOWN_OUTCOME if resolution == "verified_success" else EffectStatus.RETRYABLE_FAILURE
    store.record_effect(EffectReceipt(command.command_id, command.executor_identity, now, now, initial, error="original observation"))
    if resolution == "verified_success":
        store.resolve_unknown_effect(EffectReceipt(command.command_id, command.executor_identity, now, now, EffectStatus.SUCCEEDED))
    else:
        store.exhaust_command(EffectReceipt(command.command_id, command.executor_identity, now, now, EffectStatus.PERMANENT_FAILURE, error="ceiling"))
    with store._connect() as db:
        archived = db.execute("SELECT payload_json FROM workflow_evidence WHERE kind='prior_receipt' AND command_id=?", (str(command.command_id),)).fetchall()
    assert len(archived) == 1
    assert "original observation" in archived[0][0]


@pytest.mark.parametrize("status", [EffectStatus.SUCCEEDED, EffectStatus.UNKNOWN_OUTCOME, EffectStatus.PERMANENT_FAILURE])
def test_foreign_executor_cannot_mint_bound_effect_receipt(tmp_path, status):
    store, _state, command = fixture(tmp_path)
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    receipt = EffectReceipt(command.command_id, "other/repo:review:foreign", now, now, status,
                            error=None if status is EffectStatus.SUCCEEDED else "foreign claim")
    with pytest.raises(ControlStoreError, match="attempt owner"):
        store.record_effect(receipt)
    assert store.effect_receipt(command.command_id) is None
    assert store.pending_commands()[0].state == "executing"


def test_retired_standalone_command_cannot_escape_through_occurrence_allocator(tmp_path):
    from issue_agent.domain.workflow import RetiredSettledCommand
    store, _state, command = fixture(tmp_path)
    store.enqueue_command(command)
    with store.transaction() as db:
        db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                   (encode_workflow(RetiredSettledCommand("retirement", "never-began")), str(command.command_id)))
    with pytest.raises(ControlStoreError, match="reconciliation-only or retired"):
        store.enqueue_command_occurrence(command)
    assert len(store.pending_commands()) == 1
    assert store.pending_commands()[0].attempts == 0
