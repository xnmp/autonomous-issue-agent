"""Recovery must preserve real provenance and refuse incomplete quiescence."""
import pytest

from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.contracts import RunId
from tests.app.test_pr_repair_terminal_recovery import (
    OWNER, _dump, _launch_and_lose_return, _repair, _workflow,
)
from tests.app.test_pr_repair_workflow import issue_ci_state


def _uncaptured_settled_result(monkeypatch, workflow, files, inner):
    store = files._workflow_db
    returned = []
    original_run = inner.run

    async def launched(task):
        store.register_process_lease(RunId(task.env['ISSUE_AGENT_RUN_ID']),
            expected_owner_id=OWNER, pid=123, pgid=123, process_start_ticks=99)
        return await original_run(task)

    def lose_capture(result, writer):
        returned.append(result)
        raise RuntimeError('lost before durable capture')

    with monkeypatch.context() as patch:
        patch.setattr(inner, 'run', launched)
        patch.setattr(store, 'capture_pr_repair_result', lose_capture)
        with pytest.raises(RuntimeError, match='lost before durable capture'):
            _repair(workflow, 'rebase')
    result = returned[0]
    assert store.settle(result.settlement).disposition.value == 'available'
    return result


def _money(store):
    with store.transaction() as db:
        return {table: tuple(tuple(row) for row in db.execute(f'SELECT * FROM {table}'))
            for table in ('charges', 'budget_reservations', 'reservation_recovery', 'runs')}


def test_settled_recovery_preserves_prior_session_and_money_and_accepts_late_capture(monkeypatch):
    workflow, files, inner, key = _workflow('rebase')
    store, control = files._workflow_db, files._workflow_authority
    _repair(workflow, 'rebase')
    prior = control.read_pr_repair(key).component.state
    assert prior.session is not None and prior.actual_model is not None
    result = _uncaptured_settled_result(monkeypatch, workflow, files, inner)
    money = _money(store)
    assert control.reconcile_pr_repair_returns() == 1
    assert control.read_pr_repair(key).component.state == prior
    assert issue_ci_state(files).pending_rebase is None
    assert issue_ci_state(files).rebase_failures == 2
    assert _money(store) == money
    assert store.settle_pr_repair_result(result, control.writer).idempotent
    assert control.read_pr_repair(key).component.state == prior
    assert issue_ci_state(files).rebase_failures == 2
    assert _money(store) == money
    assert control.reconcile_pr_repair_returns() == 0


def test_disposed_money_with_retained_dispatch_claim_waits_for_claim_release(monkeypatch):
    workflow, files, inner, _ = _workflow('rebase')
    store, control = files._workflow_db, files._workflow_authority
    assert store.claim_dispatch('o/r', 'fix', 5, 'REBASE', OWNER, frozenset({'file'}))

    def bind(run_id):
        assert store.bind_dispatch_claim('o/r', 'fix', 5, OWNER, run_id)

    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, 'rebase', during=bind)
    assert store.release(run_id, 'operator disposition').disposition.value == 'available'
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held
    assert store.release_dispatch_claim('o/r', 'fix', 5, OWNER)
    assert control.reconcile_pr_repair_returns() == 1
    assert issue_ci_state(files).pending_rebase is None


def test_disposed_money_with_retained_lease_remains_held(monkeypatch):
    workflow, files, inner, _ = _workflow('rebase')
    store, control = files._workflow_db, files._workflow_authority
    leases = []

    def capture_lease(run_id):
        with store.transaction() as db:
            leases.append(tuple(db.execute('SELECT * FROM leases WHERE run_id=?', (str(run_id),)).fetchone()))

    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, 'rebase', during=capture_lease)
    lease = leases[0]
    assert store.release(run_id, 'operator disposition').disposition.value == 'available'
    # Model an inconsistent terminal marker with a still-owned process receipt.
    with store.transaction() as db:
        db.execute('INSERT INTO leases VALUES (' + ','.join('?' for _ in lease) + ')', lease)
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held
    store.release_lease(run_id)
    assert control.reconcile_pr_repair_returns() == 1


def test_acknowledgment_rolls_back_both_components_and_receipts_on_write_failure(monkeypatch):
    workflow, files, inner, _ = _workflow('rebase')
    store, control = files._workflow_db, files._workflow_authority
    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, 'rebase')
    assert store.release(run_id, 'operator disposition').disposition.value == 'available'
    held = _dump(store)
    original = store._transition

    def fail_acknowledgment(db, entity, *args, **kwargs):
        if entity == 'pr_repair':
            raise WorkflowAuthorityError('injected final acknowledgment write failure')
        return original(db, entity, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, '_transition', fail_acknowledgment)
        assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held
    assert control.reconcile_pr_repair_returns() == 1


def test_recovered_run_cannot_clear_newer_pending_admission(monkeypatch):
    workflow, files, inner, key = _workflow('rebase')
    store, control = files._workflow_db, files._workflow_authority
    old = _launch_and_lose_return(monkeypatch, workflow, files, inner, 'rebase')
    assert store.release(old, 'operator disposition').disposition.value == 'available'
    assert control.reconcile_pr_repair_returns() == 1
    newer = _launch_and_lose_return(monkeypatch, workflow, files, inner, 'rebase')
    assert newer != old
    state = issue_ci_state(files)
    assert state.pending_rebase.attempt.run_id == newer
    selected = control.read_pr_repair(key)
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held
    assert issue_ci_state(files) == state and control.read_pr_repair(key) == selected


@pytest.mark.parametrize('state', ['pending', 'executing', 'retryable_failure', 'unknown_outcome'])
def test_unresolved_publication_keeps_terminal_repair_held(monkeypatch, state):
    import hashlib
    import json

    workflow, files, inner, _ = _workflow('rebase')
    store, control = files._workflow_db, files._workflow_authority
    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, 'rebase')
    assert store.release(run_id, 'operator disposition').disposition.value == 'available'
    payload = json.dumps({'run_id': str(run_id)})
    # Retain an interrupted broker command; recovery must not race its remote effect.
    with store.transaction() as db:
        db.execute("INSERT INTO lifecycle_commands(command_id,idempotency_key,logical_key,kind,target,"
            "repository,executor_identity,payload_json,payload_digest,state,created_at,updated_at) "
            "VALUES ('pending-push','pending-push','pending-push','publish_branch','pr:100',"
            "'o/r',?,?,?,?, '2026-10-10T00:00:00Z','2026-10-10T00:00:00Z')",
            (OWNER, payload, hashlib.sha256(payload.encode()).hexdigest(), state))
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held
    with store.transaction() as db:
        db.execute("UPDATE lifecycle_commands SET state='permanent_failure' WHERE command_id='pending-push'")
    assert control.reconcile_pr_repair_returns() == 1
    assert issue_ci_state(files).pending_rebase is None
