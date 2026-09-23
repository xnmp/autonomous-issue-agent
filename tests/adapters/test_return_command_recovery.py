"""Held outbox debt stays visible; superseded debt never regains write authority."""

from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import json
import sqlite3

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.effects import GitHubEffectExecutor, action_from_command
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import workflow_scope
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.workflow import (
    ActiveReconciliation, EvidenceUnavailable, NoAuthorizedRemediation, OpenGate,
    ReconciliationOnly, RemoteOutcomeUncertain, ResolveResultKind, ResolveStage,
    RetiredSettledCommand, RetiredUncertainCommand, RetryAuthorizationRequired,
    WorkflowCommitted,
)
from tests.adapters.test_effects import Gateway
from tests.adapters.test_workflow_control import (
    NOW, WRITER, adapter, command_row, commit, paid_result, paid_return,
    request, return_observations,
)
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot


@pytest.fixture
def control(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.sqlite"), now=lambda: NOW)
    store.initialize()
    return store


def finalized(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before)
    adapter(control).capture_return(returned)
    result = adapter(control).finalize_return(returned, *return_observations(returned))
    assert isinstance(result, WorkflowCommitted)
    command = control.pending_commands(repository="o/r", executor_identity=WRITER.owner_id)[0].command
    return result.snapshot, command


def add_hold(control, snapshot):
    gate = OpenGate("another-hold", snapshot.binding.fence.generation,
                    EvidenceUnavailable("independent evidence", NOW, 1, "unavailable"),
                    NoAuthorizedRemediation("observe"))
    with control.transaction() as db:
        db.execute("INSERT INTO workflow_gates VALUES (?,?,?,?,?,?,'open',?)",
                   (gate.gate_id, *workflow_scope(snapshot.binding.fence.key), gate.opened_generation,
                    "independent", encode_workflow(gate)))
    return gate


def rearm(control, snapshot):
    for index in range(2):
        result = paid_result(control, snapshot, f"next-{index}", ResolveStage.RESOLVE)
        snapshot = adapter(control).record_attempt(replace(result, kind=ResolveResultKind.COUNTED_REQUEUE)).snapshot
    renewed = commit(control, request(snapshot))
    assert isinstance(renewed, WorkflowCommitted)
    assert renewed.snapshot.binding.fence.generation == snapshot.binding.fence.generation + 1
    return renewed.snapshot


def executor(control, gateway=None, logs=None):
    return GitHubEffectExecutor(gateway or Gateway(), control, repository="o/r", identity=WRITER.owner_id,
                                log=logs.append if logs is not None else lambda _: None)


def test_repeated_held_drains_do_not_attempt_write_or_emit_errors(control):
    snapshot, command = finalized(control)
    gate = add_hold(control, snapshot)
    gateway, logs = Gateway(), []
    drain = executor(control, gateway, logs)
    original = dict(command_row(control, command.command_id))
    for _ in range(5):
        assert drain.drain_pending_commands() == 0
    assert gateway.calls == [] and logs == []
    assert dict(command_row(control, command.command_id)) == original
    assert control.pending_commands(repository="o/r")[0].command == command
    with control.transaction() as db:
        db.execute("UPDATE workflow_gates SET state='closed' WHERE gate_id=?", (gate.gate_id,))
    selected = control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id, diagnose=logs.append)
    assert selected[0].command == command
    control.begin_command(command.command_id)
    assert command_row(control, command.command_id)["attempts"] == 1


def test_actual_rearm_retires_old_return_debt_once_without_erasing_inventory(control):
    snapshot, command = finalized(control)
    renewed = rearm(control, snapshot)
    logs = []
    control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id, diagnose=logs.append)
    retired = control.command_reconciliation(command.command_id)
    assert isinstance(retired, RetiredSettledCommand)
    with closing(control._connect()) as db:
        evidence = db.execute("SELECT payload_json FROM workflow_evidence WHERE evidence_id=?", (retired.retirement_evidence_id,)).fetchone()[0]
    assert json.loads(evidence)["superseded_by"] == encode_workflow(renewed.binding)
    for _ in range(3):
        selected = control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id, diagnose=logs.append)
        assert command.command_id not in {item.command.command_id for item in selected}
    assert logs == []
    assert command_row(control, command.command_id)["attempts"] == 0
    assert command.command_id in {item.command.command_id for item in control.pending_commands(repository="o/r")}


@pytest.mark.parametrize("present", [False, True])
def test_ambiguous_superseded_return_is_read_only_even_with_changed_guard(control, present):
    snapshot, command = finalized(control)
    control.begin_command(command.command_id)
    original = EffectReceipt(command.command_id, WRITER.owner_id, NOW, NOW, EffectStatus.UNKNOWN_OUTCOME, error="response lost")
    control.record_effect(original)
    rearm(control, snapshot)
    gateway, logs = Gateway(), []
    gateway.issue_revision = "changed-after-rearm"
    if present:
        action = action_from_command(command)
        for label in action.added_labels:
            gateway.labels.add(("issue", 1, label.value))
        if action.comment:
            gateway.comments.add(("issue", 1, action.comment))
    # Isolate the old obligation from the new operator projection.
    selected = control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id, diagnose=logs.append)
    assert command.command_id in {item.command.command_id for item in selected}
    state = control.command_reconciliation(command.command_id)
    assert isinstance(state, RetiredUncertainCommand)
    assert executor(control, gateway, logs)._reconcile_read_only(command, state) is present
    assert isinstance(control.command_reconciliation(command.command_id), RetiredSettledCommand)
    assert gateway.calls == [] and logs == []
    current = adapter(control).load_workflow(snapshot.binding.fence.key)
    assert not any(isinstance(getattr(gate, "cause", None), RemoteOutcomeUncertain) for gate in current.gates)
    assert command_row(control, command.command_id)["attempts"] == 1
    if not present:
        assert control.effect_receipt(command.command_id) == original


def test_reconciliation_only_absence_does_not_authorize_write_and_obeys_due_time(control):
    snapshot, command = finalized(control)
    control.begin_command(command.command_id)
    gate = OpenGate("uncertain", snapshot.binding.fence.generation,
                    RemoteOutcomeUncertain(command.command_id, NOW, 2, "lost"), NoAuthorizedRemediation("observe"))
    state = ReconciliationOnly(ActiveReconciliation(NOW, NOW, 2), gate.gate_id)
    with control.transaction() as db:
        db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?", (encode_workflow(state), str(command.command_id)))
        db.execute("INSERT INTO workflow_gates VALUES (?,?,?,?,?,?,'open',?)", (gate.gate_id, *workflow_scope(snapshot.binding.fence.key), gate.opened_generation, "uncertain", encode_workflow(gate)))
    gateway, logs = Gateway(), []
    drain = executor(control, gateway, logs)
    assert drain.drain_pending_commands() == 0
    after = adapter(control).load_workflow(snapshot.binding.fence.key)
    assert isinstance(after.gates[0].cause, RetryAuthorizationRequired)
    for _ in range(3):
        assert drain.drain_pending_commands() == 0
    assert gateway.calls == [] and logs == []
    assert command_row(control, command.command_id)["attempts"] == 1
    assert control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id, diagnose=logs.append) == ()


def test_corrupt_command_does_not_starve_a_healthy_command(control):
    snapshot, command = finalized(control)
    rearm(control, snapshot)
    with control.transaction() as db:
        db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json='{}' WHERE command_id=?", (str(command.command_id),))
    logs = []
    selected = control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id, diagnose=logs.append)
    assert selected and all(item.command.command_id != command.command_id for item in selected)
    assert len(logs) == 1 and str(command.command_id) in logs[0]


def test_unavailable_retired_read_preserves_uncertainty_and_backs_off(control):
    snapshot, command = finalized(control)
    control.begin_command(command.command_id)
    rearm(control, snapshot)
    control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id, diagnose=lambda _: None)
    state = control.command_reconciliation(command.command_id)
    assert isinstance(state, RetiredUncertainCommand)
    assert not control.record_reconciliation_read(command.command_id, state, executor_identity=WRITER.owner_id,
                                                 observed=None, error="remote unavailable")
    after = control.command_reconciliation(command.command_id)
    assert isinstance(after, RetiredUncertainCommand)
    assert after.uncertainty.history.unresolved_since == state.uncertainty.history.unresolved_since
    assert after.uncertainty.history.next_read_at == NOW + timedelta(seconds=30)
    selected = control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id, diagnose=lambda _: None)
    assert command.command_id not in {item.command.command_id for item in selected}


def test_retirement_transaction_failure_is_not_silently_isolated(control):
    snapshot, command = finalized(control)
    rearm(control, snapshot)
    with control.transaction() as db:
        db.execute("CREATE TRIGGER fail_retirement BEFORE INSERT ON workflow_evidence BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="disk failure"):
        control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id, diagnose=lambda _: None)
    assert control.command_reconciliation(command.command_id) is None
    assert command_row(control, command.command_id)["attempts"] == 0


def test_fabricated_retirement_cannot_silently_hide_a_command(control):
    _, command = finalized(control)
    with control.transaction() as db:
        db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                   (encode_workflow(RetiredSettledCommand("missing-retirement", "missing-outcome")), str(command.command_id)))
    logs = []
    assert control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id, diagnose=logs.append) == ()
    assert len(logs) == 1 and "retirement/outcome evidence" in logs[0]
    assert control.pending_commands(repository="o/r")[0].command == command


def test_reconciliation_cannot_close_an_unrelated_gate(control):
    snapshot, command = finalized(control)
    control.begin_command(command.command_id)
    gate = add_hold(control, snapshot)
    state = ReconciliationOnly(ActiveReconciliation(NOW, NOW, 0), gate.gate_id)
    with control.transaction() as db:
        db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?", (encode_workflow(state), str(command.command_id)))
    logs = []
    assert executor(control, Gateway(), logs).drain_pending_commands() == 0
    assert len(logs) == 1 and "matching visible gate" in logs[0]
    assert adapter(control).load_workflow(snapshot.binding.fence.key).gates == (gate,)


def test_supersession_cannot_overflow_the_validated_gate_bound(control):
    snapshot, command = finalized(control)
    control.begin_command(command.command_id)
    renewed = rearm(control, snapshot)
    with control.transaction() as db:
        for index in range(63):
            gate = OpenGate(f"hold-{index}", renewed.binding.fence.generation,
                            EvidenceUnavailable(f"fact-{index}", NOW, 1, "unavailable"),
                            NoAuthorizedRemediation("observe"))
            db.execute("INSERT INTO workflow_gates VALUES (?,?,?,?,?,?,'open',?)",
                       (gate.gate_id, *workflow_scope(snapshot.binding.fence.key), gate.opened_generation,
                        f"fact-{index}", encode_workflow(gate)))
    before = adapter(control).load_workflow(snapshot.binding.fence.key)
    assert len(before.gates) == 64
    logs = []
    control.runnable_commands(repository="o/r", executor_identity=WRITER.owner_id, diagnose=logs.append)
    assert len(logs) == 1
    assert adapter(control).load_workflow(snapshot.binding.fence.key) == before
    assert control.command_reconciliation(command.command_id) is None


@pytest.mark.parametrize("failure_point", ["command_reconciliation", "record_reconciliation_read"])
def test_drain_propagates_sqlite_failure_after_selection(control, monkeypatch, failure_point):
    snapshot, command = finalized(control)
    if failure_point == "record_reconciliation_read":
        control.begin_command(command.command_id)
        rearm(control, snapshot)

    def fail_storage(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SQLiteControlStore, failure_point, fail_storage)
    logs = []
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        executor(control, Gateway(), logs).drain_pending_commands()
    assert not any("disk I/O error" in line for line in logs)
