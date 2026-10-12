"""A terminal, quiescent repair with unknown cost no longer freezes its PR.

Tauri PRs #1019/#1001/#999: legacy accounting holds settle before acknowledgment.
"""
import hashlib
import json

import pytest

from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.domain import continuation_components as cc
from tests.app.test_pr_repair_terminal_recovery import (
    OWNER, _dump, _failures, _launch_and_lose_return, _pending, _repair, _workflow,
)
from tests.app.test_pr_repair_workflow import issue_ci_state


def _money(store, run_id):
    with store.transaction() as db:
        return tuple(tuple(tuple(r) for r in db.execute(f"SELECT * FROM {t} WHERE run_id=?", (str(run_id),)).fetchall())
                     for t in ("budget_reservations", "reservation_recovery"))


def _active_reserved(store):
    with store.transaction() as db:
        return db.execute("SELECT COUNT(*) FROM budget_reservations WHERE state='active'").fetchone()[0]


def _lose_with_unknown_cost(monkeypatch, lane="review", during=None):
    workflow, files, inner, key = _workflow(lane)
    store = files._workflow_db
    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, lane, during=during)
    with store.transaction() as db:
        db.execute("UPDATE runs SET outcome='accounting_unavailable' WHERE run_id=?", (str(run_id),))
    with store.transaction() as db:
        # Incident disposition: terminal, process gone, retained as reconcile_only.
        db.execute("UPDATE reservation_recovery SET policy='reconcile_only' WHERE run_id=?", (str(run_id),))
    assert store.settle_terminal_estimate(run_id, "legacy request evidence unavailable")
    return workflow, files, inner, key, run_id


def _receipt(store, control, key, run_id):
    receipt_key = store._continuation_receipt_key(run_id, control.read_pr_repair(key).binding.authority.family)
    with store.transaction() as db:
        return db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND key=?",
            (receipt_key,)).fetchone()


@pytest.mark.parametrize("lane", ["review", "rebase", "ci"])
def test_unknown_cost_run_is_acknowledged_and_next_repair_proceeds_after_settlement(monkeypatch, lane):
    workflow, files, inner, key, run_id = _lose_with_unknown_cost(monkeypatch, lane)
    store, control = files._workflow_db, files._workflow_authority
    before_money, reserved = _money(store, run_id), _active_reserved(store)
    with store.transaction() as db:
        assert db.execute("SELECT state FROM budget_reservations WHERE run_id=?", (str(run_id),)).fetchone()[0] == "settled"
    failures = _failures(issue_ci_state(files), lane)

    assert control.reconcile_pr_repair_returns() == 1
    assert _money(store, run_id) == before_money  # not released, settled or reclassified
    assert _active_reserved(store) == reserved
    assert _pending(issue_ci_state(files), lane) is None
    assert _failures(issue_ci_state(files), lane) == failures  # unreturned is not an attempt
    assert isinstance(decode_workflow(_receipt(store, control, key, run_id)[0], tuple)[1], cc.PRRepairUnreturned)

    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held

    _repair(workflow, lane)
    assert len(inner.tasks) == 1
    # The retained unknown spend still counts alongside the new repair's money.
    assert _money(store, run_id) == before_money
    assert _failures(issue_ci_state(files), lane) == failures + 1


def test_later_release_of_retained_run_keeps_pr_consistent(monkeypatch):
    workflow, files, inner, key, run_id = _lose_with_unknown_cost(monkeypatch)
    store, control = files._workflow_db, files._workflow_authority
    assert control.reconcile_pr_repair_returns() == 1
    state, repair = issue_ci_state(files), control.read_pr_repair(key)
    assert store.settle_terminal_estimate(run_id, "repeat recovery")
    assert control.reconcile_pr_repair_returns() == 0
    assert issue_ci_state(files) == state and control.read_pr_repair(key) == repair
    _repair(workflow, "review")
    assert len(inner.tasks) == 1


def test_retained_run_with_live_lease_still_holds(monkeypatch):
    workflow, files, inner, key = _workflow("review")
    store, control = files._workflow_db, files._workflow_authority
    leases = []

    def capture(rid):
        with store.transaction() as db:
            leases.append(tuple(db.execute("SELECT * FROM leases WHERE run_id=?", (str(rid),)).fetchone()))

    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, "review", during=capture)
    with store.transaction() as db:
        db.execute("UPDATE runs SET outcome='accounting_unavailable' WHERE run_id=?", (str(run_id),))
    with store.transaction() as db:
        db.execute("UPDATE reservation_recovery SET policy='reconcile_only' WHERE run_id=?", (str(run_id),))
        db.execute("INSERT INTO leases VALUES (" + ",".join("?" for _ in leases[0]) + ")", leases[0])
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held
    assert not store.settle_terminal_estimate(run_id, "process may be live")
    store.release_lease(run_id)
    assert store.settle_terminal_estimate(run_id, "process reaped")
    assert control.reconcile_pr_repair_returns() == 1


def test_retained_run_with_dispatch_claim_still_holds(monkeypatch):
    workflow, files, inner, key = _workflow("review")
    store, control = files._workflow_db, files._workflow_authority
    assert store.claim_dispatch("o/r", "fix", 5, "REBASE", OWNER, frozenset({"file"}))

    def bind(rid):
        assert store.bind_dispatch_claim("o/r", "fix", 5, OWNER, rid)

    run_id = _launch_and_lose_return(monkeypatch, workflow, files, inner, "review", during=bind)
    with store.transaction() as db:
        db.execute("UPDATE runs SET outcome='accounting_unavailable' WHERE run_id=?", (str(run_id),))
    with store.transaction() as db:
        db.execute("UPDATE reservation_recovery SET policy='reconcile_only' WHERE run_id=?", (str(run_id),))
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held
    assert store.release_dispatch_claim("o/r", "fix", 5, OWNER)
    assert store.settle_terminal_estimate(run_id, "request evidence unavailable")
    assert control.reconcile_pr_repair_returns() == 1


@pytest.mark.parametrize("state", ["pending", "executing", "retryable_failure", "unknown_outcome"])
def test_retained_run_with_unresolved_publication_still_holds(monkeypatch, state):
    workflow, files, inner, key, run_id = _lose_with_unknown_cost(monkeypatch)
    store, control = files._workflow_db, files._workflow_authority
    payload = json.dumps({"run_id": str(run_id)})
    with store.transaction() as db:
        db.execute("INSERT INTO lifecycle_commands(command_id,idempotency_key,logical_key,kind,target,"
            "repository,executor_identity,payload_json,payload_digest,state,created_at,updated_at) "
            "VALUES ('pending-push','pending-push','pending-push','publish_branch','pr:100',"
            "'o/r',?,?,?,?, '2026-10-10T00:00:00Z','2026-10-10T00:00:00Z')",
            (OWNER, payload, hashlib.sha256(payload.encode()).hexdigest(), state))
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held


def test_retained_run_with_captured_result_is_left_to_the_ordinary_path(monkeypatch):
    workflow, files, inner, key, run_id = _lose_with_unknown_cost(monkeypatch)
    store, control = files._workflow_db, files._workflow_authority
    receipt_key = store._continuation_receipt_key(run_id, control.read_pr_repair(key).binding.authority.family)
    with store.transaction() as db:
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('pr-repair-result',?,?,?,?)",
            ("o/r", receipt_key, "2026-10-10T00:00:00Z", "{}"))
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held


@pytest.mark.parametrize("mutation", [
    "UPDATE runs SET started_at=NULL WHERE run_id=?",
    "UPDATE runs SET outcome='unlaunched_released' WHERE run_id=?",
    "UPDATE reservation_recovery SET policy='spawn_pending' WHERE run_id=?",
    "DELETE FROM reservation_recovery WHERE run_id=?",
    "UPDATE runs SET state='running' WHERE run_id=?",
])
def test_unlaunched_unknown_or_malformed_recovery_fails_closed(monkeypatch, mutation):
    workflow, files, inner, key, run_id = _lose_with_unknown_cost(monkeypatch)
    store, control = files._workflow_db, files._workflow_authority
    with store.transaction() as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(mutation, (str(run_id),))
    held = _dump(store)
    assert control.reconcile_pr_repair_returns() == 0
    assert _dump(store) == held
    assert _pending(issue_ci_state(files), "review") is not None


@pytest.mark.parametrize("retained", [True, False])
def test_acknowledgment_journals_whether_money_is_still_retained(monkeypatch, retained):
    workflow, files, inner, key, run_id = _lose_with_unknown_cost(monkeypatch)
    store, control = files._workflow_db, files._workflow_authority
    if not retained:
        assert store.settle_terminal_estimate(run_id, "repeat recovery")
    journal = []
    original = store._transition

    def record(db, entity, entity_id, state, *args, **kwargs):
        journal.append((entity, entity_id, state))
        return original(db, entity, entity_id, state, *args, **kwargs)

    monkeypatch.setattr(store, "_transition", record)
    assert control.reconcile_pr_repair_returns() == 1
    expected = "unreturned_acknowledged"
    assert journal == [("pr_repair", str(run_id), expected)]
