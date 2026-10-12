"""Raw child evidence and accounting settle together before round decisions."""

from dataclasses import replace
from datetime import datetime, timedelta
import sqlite3

import pytest

from issue_agent.adapters.control_store import AccountingDisposition, ControlStoreError, SettlementRequest
from issue_agent.adapters.workflow_codec import WorkflowEncodingError, encode_workflow
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.contracts import Money, PricingBasis
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.review_return import MAX_RETURN_EVIDENCE_BYTES, ReturnedReviewChild, review_returned_envelope
from issue_agent.domain.review_rounds import ReviewPlanningComplete, ReviewPlanningSealed
from tests.adapters.test_review_execution_admission import planned, request
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig
from tests.adapters.test_review_execution_launch import admitted, launch_receipt, register


def returned(store, proposed, *, result=None):
    result = result or AgentResult(False, status="APPROVE", summary="Reviewed café\nexact", session_id="session:1",
                                   structured={"decision": "APPROVE", "body": "Reviewed café\nexact"}, exit_reason=ExitReason.OK)
    return ReturnedReviewChild(proposed.review_child, str(proposed.run_id), OWNER, RuntimeName.CLAUDE,
                               review_returned_envelope(result), store.now())


def settlement(proposed, *, amount=12, basis=PricingBasis.PROVIDER_REPORTED):
    return SettlementRequest(proposed.run_id, Money("USD", amount), basis, f"provider:{proposed.run_id}", None, None, None)


def read_return(store, proposed):
    return store.returned_review_child("o/r", proposed.run_id)


def test_launched_return_commits_exact_output_and_charge_without_projecting_the_round(admitted):
    store, selection, proposed = admitted
    register(store, proposed)
    launch = launch_receipt(store)
    snapshot = current(store, selection)
    evidence = returned(store, proposed)
    assert read_return(store, proposed) is None
    result = store.settle_review_return(settlement(proposed), evidence)
    assert result.disposition is AccountingDisposition.AVAILABLE and not result.idempotent
    assert read_return(store, proposed) == evidence
    assert current(store, selection) == snapshot
    assert launch_receipt(store) == launch and store.leases() == ()
    balance = store.snapshot(proposed.scope)
    assert balance.reserved.micros == 0 and balance.committed.micros == 12
    with store.transaction() as db:
        assert tuple(db.execute("SELECT state,provider_receipt_id FROM runs").fetchone()) == ("completed", "provider:run-1")
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "settled"
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 1
        assert tuple(db.execute("SELECT closure_json,outcome_json FROM review_execution_rounds").fetchone()) == (None, None)


def test_exact_return_and_settlement_replay_are_inert(admitted):
    store, _, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    paid = settlement(proposed)
    assert store.settle_review_return(paid, evidence).disposition is AccountingDisposition.AVAILABLE
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    before = dump(store)
    replay = store.settle_review_return(paid, evidence)
    assert replay.disposition is AccountingDisposition.AVAILABLE and replay.idempotent
    assert read_return(store, proposed) == evidence and dump(store) == before


@pytest.mark.parametrize("change", ["output", "runtime", "child", "money"])
def test_conflicting_return_or_money_cannot_rewrite_accepted_evidence(admitted, change):
    store, _, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    assert store.settle_review_return(settlement(proposed), evidence).disposition is AccountingDisposition.AVAILABLE
    changed = (replace(evidence, result=replace(evidence.result, summary="changed")) if change == "output" else
               replace(evidence, runtime=RuntimeName.CODEX) if change == "runtime" else
               replace(evidence, child=replace(evidence.child, child_id="foreign")) if change == "child" else evidence)
    before = dump(store)
    if change == "money":
        assert store.settle_review_return(settlement(proposed, amount=13), changed).disposition is AccountingDisposition.CONFLICT
    else:
        with pytest.raises(ControlStoreError):
            store.settle_review_return(settlement(proposed), changed)
    assert read_return(store, proposed) == evidence and dump(store) == before


def test_indeterminate_spend_retains_money_and_raw_return_until_reconciliation(admitted, monkeypatch):
    store, selection, proposed = admitted
    register(store, proposed)
    snapshot = current(store, selection)
    evidence = returned(store, proposed)
    assert store.record_reservation_retained(proposed.run_id, "usage unavailable", returned_review=evidence)
    assert read_return(store, proposed) == evidence and store.leases() == ()
    assert current(store, selection) == snapshot
    assert store.snapshot(proposed.scope).reserved.micros == 60
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT policy FROM reservation_recovery").fetchone()[0] == "reconcile_only"
    before = dump(store)
    retry_at = store.now() + timedelta(hours=1)
    monkeypatch.setattr(store, "_now", lambda: retry_at)
    assert store.record_reservation_retained(proposed.run_id, "usage unavailable", returned_review=evidence)
    assert dump(store) == before
    result = store.settle_review_return(settlement(proposed, basis=PricingBasis.RECONCILED), evidence)
    assert result.disposition is AccountingDisposition.AVAILABLE
    assert store.snapshot(proposed.scope).reserved.micros == 0
    assert store.snapshot(proposed.scope).committed.micros == 12
    assert read_return(store, proposed) == evidence


def test_retention_reason_change_updates_recovery_without_rewriting_completion(admitted, monkeypatch):
    store, _, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    assert store.record_reservation_retained(proposed.run_id, "usage unavailable", returned_review=evidence)
    with store.transaction() as db:
        run_before = tuple(db.execute("SELECT * FROM runs WHERE run_id=?", (str(proposed.run_id),)).fetchone())
    retry_at = store.now() + timedelta(hours=1)
    monkeypatch.setattr(store, "_now", lambda: retry_at)
    assert store.record_reservation_retained(proposed.run_id, "provider reconciliation pending", returned_review=evidence)
    with store.transaction() as db:
        assert tuple(db.execute("SELECT * FROM runs WHERE run_id=?", (str(proposed.run_id),)).fetchone()) == run_before
        recovery = db.execute("SELECT policy,reason,updated_at FROM reservation_recovery WHERE run_id=?",
                              (str(proposed.run_id),)).fetchone()
        assert tuple(recovery[:2]) == ("reconcile_only", "provider reconciliation pending")
        assert datetime.fromisoformat(recovery["updated_at"].replace("Z", "+00:00")) == retry_at
    assert read_return(store, proposed) == evidence
    assert store.snapshot(proposed.scope).reserved.micros == 60


def test_reconciler_first_still_captures_exact_return_without_changing_settled_money(admitted):
    store, selection, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    assert store.settle(settlement(proposed, basis=PricingBasis.RECONCILED)).disposition is AccountingDisposition.AVAILABLE
    assert read_return(store, proposed) is None
    snapshot = current(store, selection)
    balance = store.snapshot(proposed.scope)
    with store.transaction() as db:
        charge = tuple(db.execute("SELECT * FROM charges").fetchone())
        run = tuple(db.execute("SELECT * FROM runs").fetchone())
    assert not store.record_reservation_retained(proposed.run_id, "runtime usage unavailable", returned_review=evidence)
    assert read_return(store, proposed) == evidence
    assert store.snapshot(proposed.scope) == balance and current(store, selection) == snapshot
    with store.transaction() as db:
        assert tuple(db.execute("SELECT * FROM charges").fetchone()) == charge
        assert tuple(db.execute("SELECT * FROM runs").fetchone()) == run
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "settled"
    before = dump(store)
    assert not store.record_reservation_retained(proposed.run_id, "runtime usage unavailable", returned_review=evidence)
    assert dump(store) == before
    with pytest.raises(ControlStoreError):
        store.record_reservation_retained(proposed.run_id, "runtime usage unavailable",
                                         returned_review=replace(evidence, result=replace(evidence.result, summary="changed")))
    assert dump(store) == before


@pytest.mark.parametrize("corruption", ["runtime", "child"])
def test_reconciler_first_does_not_accept_return_for_other_work(admitted, corruption):
    store, _, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    assert store.settle(settlement(proposed, basis=PricingBasis.RECONCILED)).disposition is AccountingDisposition.AVAILABLE
    changed = (replace(evidence, runtime=RuntimeName.CODEX) if corruption == "runtime" else
               replace(evidence, child=replace(evidence.child, child_id="foreign")))
    before = dump(store)
    with pytest.raises(ControlStoreError):
        store.record_reservation_retained(proposed.run_id, "runtime usage unavailable", returned_review=changed)
    assert read_return(store, proposed) is None and dump(store) == before


@pytest.mark.parametrize("change", ["generation", "sealed_plan", "released_claim"])
def test_historical_return_preserves_actual_spend_after_current_authority_changes(admitted, change):
    store, _, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    if change == "released_claim":
        assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    else:
        with store.transaction() as db:
            if change == "generation":
                db.execute("UPDATE workflows SET generation=generation+1,version=version+1 WHERE target_kind='pr'")
            else:
                db.execute("UPDATE review_execution_rounds SET planning_json=?",
                           (encode_workflow(ReviewPlanningSealed("closed-planning", ReviewPlanningComplete())),))
    with store.transaction() as db:
        authority_before = tuple(db.execute("SELECT generation,version,episode_json FROM workflows ORDER BY target_kind").fetchall())
    assert store.settle_review_return(settlement(proposed), evidence).disposition is AccountingDisposition.AVAILABLE
    assert read_return(store, proposed) == evidence and store.snapshot(proposed.scope).committed.micros == 12
    with store.transaction() as db:
        assert tuple(db.execute("SELECT generation,version,episode_json FROM workflows ORDER BY target_kind").fetchall()) == authority_before


@pytest.mark.parametrize("corruption", ["missing_admission", "missing_launch", "forged_launch", "missing_stage"])
def test_unproven_return_cannot_settle_money_or_create_output_authority(admitted, corruption):
    store, _, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    launch = launch_receipt(store)
    with store.transaction() as db:
        if corruption == "forged_launch":
            altered = replace(launch, admission=replace(launch.admission, claim_occurrence_id="forged"))
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-launch'", (encode_workflow(altered),))
        else:
            namespace = {"missing_admission": "review-execution-admission", "missing_launch": "review-execution-launch",
                         "missing_stage": "review-round-stage"}[corruption]
            db.execute("DELETE FROM dedupe_keys WHERE namespace=?", (namespace,))
    before = dump(store)
    with pytest.raises((ControlStoreError, ValueError)):
        store.settle_review_return(settlement(proposed), evidence)
    assert read_return(store, proposed) is None and dump(store) == before


@pytest.mark.parametrize("retained", [False, True])
def test_output_receipt_failure_rolls_back_money_completion_and_lease_cleanup(admitted, retained):
    store, _, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_review_return BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-execution-return' BEGIN SELECT RAISE(ABORT,'return refused'); END")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError, match="return refused"):
        if retained:
            store.record_reservation_retained(proposed.run_id, "unknown usage", returned_review=evidence)
        else:
            store.settle_review_return(settlement(proposed), evidence)
    assert read_return(store, proposed) is None and dump(store) == before


def test_proven_prelaunch_runtime_failure_can_capture_zero_settlement_without_inventing_launch(admitted):
    store, selection, proposed = admitted
    snapshot = current(store, selection)
    evidence = returned(store, proposed, result=AgentResult(True, exit_reason=ExitReason.RUNTIME_UNAVAILABLE))
    assert store.settle_review_return(settlement(proposed, amount=0, basis=PricingBasis.SYNTHETIC), evidence).disposition is AccountingDisposition.AVAILABLE
    assert read_return(store, proposed) == evidence and launch_receipt(store) is None
    assert current(store, selection) == snapshot
    assert store.snapshot(proposed.scope).committed.micros == 0


@pytest.mark.parametrize("field,value", [("summary", "\x00"), ("summary", "é" * MAX_RETURN_EVIDENCE_BYTES),
                                         ("session_id", "s" * 513), ("structured", {"invalid": float("nan")})])
def test_malformed_or_oversized_output_cannot_reach_durable_return_capture(admitted, field, value):
    store, _, proposed = admitted
    before = dump(store)
    with pytest.raises(ValueError):
        returned(store, proposed, result=replace(AgentResult(False), **{field: value}))
    assert read_return(store, proposed) is None and dump(store) == before


def test_unreadable_persisted_return_is_not_a_successful_replay(admitted):
    store, _, proposed = admitted
    evidence = returned(store, proposed)
    assert store.settle_review_return(settlement(proposed), evidence).disposition is AccountingDisposition.AVAILABLE
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json='unreadable' WHERE namespace='review-execution-return'")
    before = dump(store)
    with pytest.raises((ControlStoreError, WorkflowEncodingError)):
        read_return(store, proposed)
    with pytest.raises((ControlStoreError, WorkflowEncodingError)):
        store.settle_review_return(settlement(proposed), evidence)
    assert dump(store) == before


def test_forged_stored_runtime_cannot_authorize_lookup_or_its_own_replay(admitted):
    store, _, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    assert store.settle_review_return(settlement(proposed), evidence).disposition is AccountingDisposition.AVAILABLE
    forged = replace(evidence, runtime=RuntimeName.CODEX)
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-return'", (encode_workflow(forged),))
    before = dump(store)
    with pytest.raises(ControlStoreError):
        read_return(store, proposed)
    with pytest.raises(ControlStoreError):
        store.settle_review_return(settlement(proposed), forged)
    assert dump(store) == before


def test_settled_launch_remains_required_after_lease_cleanup(admitted):
    store, _, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    assert store.settle_review_return(settlement(proposed), evidence).disposition is AccountingDisposition.AVAILABLE
    assert store.leases() == () and read_return(store, proposed) == evidence
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-launch'")
    before = dump(store)
    with pytest.raises(ControlStoreError):
        read_return(store, proposed)
    with pytest.raises(ControlStoreError):
        store.settle_review_return(settlement(proposed), evidence)
    assert dump(store) == before


@pytest.mark.parametrize("field", ["pid", "pgid", "process_start_ticks"])
def test_return_cannot_launder_a_launch_receipt_that_disagrees_with_the_live_lease(admitted, field):
    store, _, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    original = launch_receipt(store)
    corrupted = replace(original, **{field: getattr(original, field) + 1})
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-launch'",
                   (encode_workflow(corrupted),))
    before = dump(store)
    with pytest.raises(ControlStoreError):
        store.settle_review_return(settlement(proposed), evidence)
    assert read_return(store, proposed) is None and dump(store) == before
    assert len(store.leases()) == 1


def test_launch_registration_time_must_match_retained_run_after_lease_cleanup(admitted):
    store, _, proposed = admitted
    register(store, proposed)
    evidence = returned(store, proposed)
    assert store.settle_review_return(settlement(proposed), evidence).disposition is AccountingDisposition.AVAILABLE
    assert store.leases() == ()
    original = launch_receipt(store)
    corrupted = replace(original, registered_at=original.registered_at + timedelta(seconds=1))
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-launch'",
                   (encode_workflow(corrupted),))
    before = dump(store)
    with pytest.raises(ControlStoreError):
        read_return(store, proposed)
    with pytest.raises(ControlStoreError):
        store.settle_review_return(settlement(proposed), evidence)
    assert dump(store) == before
