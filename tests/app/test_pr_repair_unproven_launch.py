"""Returned preflight failures settle through their normal repair receipts."""
from dataclasses import replace

from issue_agent.domain.contracts import RunId
from issue_agent.domain.run_usage import EstimatedRunUsage
from tests.app.test_pr_repair_terminal_recovery import _dump, _repair, _workflow
from tests.app.test_pr_repair_workflow import issue_ci_state


def test_preflight_return_without_launch_settles_without_unreturned_acknowledgment():
    workflow, files, inner, _ = _workflow('rebase')
    store, control = files._workflow_db, files._workflow_authority
    # Model a preflight failure: the runtime returned without registering a process.
    inner.result = replace(inner.result, is_error=True, status='', session_id='',
        run_usage=EstimatedRunUsage('provider never started; usage unavailable'))
    _repair(workflow, 'rebase')
    run_id = RunId(inner.tasks[0].env['ISSUE_AGENT_RUN_ID'])
    with store.transaction() as db:
        run = db.execute('SELECT state,started_at,policy FROM runs '
            'JOIN reservation_recovery USING(run_id) WHERE run_id=?', (str(run_id),)).fetchone()
        assert tuple(run) == ('completed', None, 'spawn_pending')
        assert tuple(db.execute('SELECT amount_micros,basis FROM charges WHERE run_id=?',
                                (str(run_id),)).fetchone()) == (0, 'estimate')
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0] == 1
    assert issue_ci_state(files).pending_rebase is None
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held
    assert len(inner.tasks) == 1
