"""Deterministic interruptions at terminal-repair recovery transaction seams."""
import pytest

from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.domain import continuation_components as cc
from tests.app.test_pr_repair_terminal_recovery import (
    _dump, _launch_and_lose_return, _workflow,
)
from tests.app.test_pr_repair_terminal_recovery_guards import (
    _money, _uncaptured_settled_result,
)
from tests.app.test_pr_repair_workflow import issue_ci_state


def test_capture_between_selection_and_acknowledgment_wins(monkeypatch):
    workflow, files, inner, key = _workflow('rebase')
    store, control = files._workflow_db, files._workflow_authority
    result = _uncaptured_settled_result(monkeypatch, workflow, files, inner)
    before_issue, before_repair = issue_ci_state(files), control.read_pr_repair(key)
    money = _money(store)
    acknowledge = store._acknowledge_unreturned_pr_repair
    acknowledgments = []

    def capture_then_acknowledge(run_id, writer):
        # This callback is reached only after the selection transaction closes.
        assert run_id == result.admission.attempt.run_id
        store.capture_pr_repair_result(result, writer)
        captured = _dump(store)
        acknowledged = acknowledge(run_id, writer)
        acknowledgments.append(acknowledged)
        assert acknowledged is False
        assert _dump(store) == captured
        return acknowledged

    with monkeypatch.context() as patch:
        patch.setattr(store, '_acknowledge_unreturned_pr_repair', capture_then_acknowledge)
        assert control.reconcile_pr_repair_returns() == 0
    assert acknowledgments == [False]
    assert issue_ci_state(files) == before_issue
    assert control.read_pr_repair(key) == before_repair

    # The next ordinary pass must consume the real output, not an invented return.
    assert control.reconcile_pr_repair_returns() == 1
    after_issue, after_repair = issue_ci_state(files), control.read_pr_repair(key)
    assert after_issue.pending_rebase is None
    assert after_issue.rebase_failures == before_issue.rebase_failures + 1
    assert after_repair.component.state == result.state
    assert after_repair.component.state.session is not None
    assert after_repair.component.state.session.source == result.admission
    assert after_repair.component.watermarks.last_applied == result.admission.admission_version
    with store.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result'").fetchone()[0]
    assert decode_workflow(raw, cc.PRRepairResult) == result
    assert _money(store) == money
    completed = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == completed


@pytest.mark.parametrize('after_commit', [False, True], ids=['after-selection', 'after-commit'])
def test_interrupted_acknowledgment_retries_without_inventing_debt(monkeypatch, after_commit):
    workflow, files, inner, key = _workflow('rebase')
    store, control = files._workflow_db, files._workflow_authority
    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, 'rebase')
    assert store.release(run_id, 'operator disposition').disposition.value == 'available'
    before_issue, before_repair = issue_ci_state(files), control.read_pr_repair(key)
    before = _dump(store)
    money = _money(store)
    acknowledge = store._acknowledge_unreturned_pr_repair
    committed = []

    def interrupt(selected_run, writer):
        assert selected_run == run_id
        if after_commit:
            assert acknowledge(selected_run, writer) is True
            committed.append(_dump(store))
        raise RuntimeError('recovery interrupted before delivering return')

    with monkeypatch.context() as patch:
        patch.setattr(store, '_acknowledge_unreturned_pr_repair', interrupt)
        with pytest.raises(RuntimeError, match='recovery interrupted'):
            control.reconcile_pr_repair_returns()
    assert _dump(store) == (committed[0] if after_commit else before)
    assert control.reconcile_pr_repair_returns() == (0 if after_commit else 1)
    after_issue, after_repair = issue_ci_state(files), control.read_pr_repair(key)
    assert after_issue.pending_rebase is None
    assert after_issue.rebase_failures == before_issue.rebase_failures
    assert after_repair.component.state == before_repair.component.state
    assert after_repair.component.watermarks.last_applied == after_repair.component.watermarks.last_admitted
    assert _money(store) == money
    completed = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == completed
