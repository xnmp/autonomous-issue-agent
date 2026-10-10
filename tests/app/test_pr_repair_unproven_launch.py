"""Retained accounting uncertainty is not evidence that a repair launched."""
from dataclasses import replace

from issue_agent.domain.contracts import RunId
from issue_agent.domain.run_usage import UnavailableRunUsage
from tests.app.test_pr_repair_terminal_recovery import _dump, _repair, _workflow
from tests.app.test_pr_repair_workflow import issue_ci_state


def test_disposed_preflight_return_without_launch_evidence_is_not_acknowledged():
    workflow, files, inner, _ = _workflow('rebase')
    store, control = files._workflow_db, files._workflow_authority
    # Model a preflight failure: the runtime returned without registering a process.
    inner.result = replace(inner.result, is_error=True, status='', session_id='',
        run_usage=UnavailableRunUsage('provider never started; usage unavailable'))
    _repair(workflow, 'rebase')
    run_id = RunId(inner.tasks[0].env['ISSUE_AGENT_RUN_ID'])
    with store.transaction() as db:
        run = db.execute('SELECT state,started_at,policy FROM runs '
            'JOIN reservation_recovery USING(run_id) WHERE run_id=?', (str(run_id),)).fetchone()
        assert tuple(run) == ('completed', None, 'reconcile_only')
    assert store.release(run_id, 'explicit unknown-spend disposition').disposition.value == 'available'
    pending = issue_ci_state(files).pending_rebase
    assert pending is not None and pending.attempt.run_id == run_id
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held
    _repair(workflow, 'rebase')
    assert len(inner.tasks) == 1
    assert issue_ci_state(files).pending_rebase == pending
