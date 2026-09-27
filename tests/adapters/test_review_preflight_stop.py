"""Harness preflight stops remain distinct from launched and settled children."""

import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import sqlite3

import pytest

from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.codex_runner import CodexRunner
from issue_agent.adapters.control_store import AccountingDisposition, ControlStoreError
from issue_agent.adapters.journal import JournalingRuntime
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.adapters.spend_recovery import CodexSpendReconciler
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.contracts import Money, RunId
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.pricing import BudgetPolicy
from issue_agent.domain.review_return import ReviewRuntimePreflightStop, ReturnedReviewChild, review_returned_envelope
from tests.adapters.test_review_execution_admission import planned, receipt, request
from tests.adapters.test_review_execution_control import OWNER, current, dump, rig
from tests.adapters.test_review_execution_launch import register
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_execution_return import settlement
from tests.adapters.test_accounting_runtime import _rates
from tests.fakes.legacy_journal import Journal


@pytest.fixture
def codex_planned(rig):
    store, _, _, selection, control = rig
    selection = replace(selection, runtime=RuntimeName.CODEX)
    plan = control.preflight(selection)
    proposed = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, proposed)
    return store, selection, proposed.children[0]


@pytest.fixture
def codex_admitted(codex_planned):
    store, selection, child = codex_planned
    proposed = request(child)
    dispatch = json.loads(proposed.dispatch_id) | {"runtime": "codex"}
    proposed = replace(proposed, dispatch_id=json.dumps(dispatch))
    assert store.reserve(proposed).admitted
    return store, selection, proposed


def failure():
    return review_returned_envelope(AgentResult(True, status="blocked", summary="Codex executable is unavailable",
                                              exit_reason=ExitReason.RUNTIME_UNAVAILABLE))


def stop(store, proposed, result=None, owner=OWNER):
    return store.record_review_runtime_preflight_stop(proposed.run_id, owner, result or failure())


def stored_stop(store, run_id="run-1"):
    with store.transaction() as db:
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-preflight-stop' "
                         "AND scope='o/r' AND key=?", (run_id,)).fetchone()
    return decode_workflow(row[0], ReviewRuntimePreflightStop) if row else None


def assert_pending(store):
    with store.transaction() as db:
        assert tuple(db.execute("SELECT state,started_at FROM runs").fetchone()) == ("reserved", None)
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "active"
        assert db.execute("SELECT policy FROM reservation_recovery").fetchone()[0] == "spawn_pending"
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0


def test_exact_preflight_stop_is_retained_and_replays_without_mutation(codex_admitted):
    store, selection, proposed = codex_admitted
    snapshot = current(store, selection)
    accepted = stop(store, proposed)
    assert accepted == ReviewRuntimePreflightStop(receipt(store), "review-execution-preflight-stop:run-1", failure(), store.now())
    assert stored_stop(store) == accepted and current(store, selection) == snapshot
    assert_pending(store)
    before = dump(store)
    assert stop(store, proposed) == accepted and dump(store) == before
    with pytest.raises(ControlStoreError):
        stop(store, proposed, replace(failure(), summary="different failure"))
    assert dump(store) == before


def test_wrong_owner_cannot_record_a_preflight_stop(codex_admitted):
    store, _, proposed = codex_admitted
    before = dump(store)
    with pytest.raises((ControlStoreError, ValueError)):
        stop(store, proposed, owner="o/r:review:other")
    assert stored_stop(store) is None and dump(store) == before


def test_codex_preflight_stop_cannot_be_minted_for_a_claude_plan(planned):
    store, _, _, child = planned
    proposed = request(child)
    assert store.reserve(proposed).admitted
    before = dump(store)
    with pytest.raises(ControlStoreError):
        stop(store, proposed)
    assert stored_stop(store) is None and dump(store) == before


@pytest.mark.parametrize("field,value", [("is_error", False), ("status", "APPROVE"), ("summary", ""),
                                         ("exit_reason", ExitReason.TRANSPORT_DEATH), ("session_id", "session"),
                                         ("structured_json", '{"decision":"APPROVE"}'),
                                         ("needs", "human decision"), ("envelope_subtype", "success")])
def test_provider_result_cannot_claim_harness_preflight_stop(codex_admitted, field, value):
    store, _, proposed = codex_admitted
    before = dump(store)
    with pytest.raises(ValueError):
        stop(store, proposed, replace(failure(), **{field: value}))
    assert stored_stop(store) is None and dump(store) == before


def test_preflight_stop_winning_first_prevents_guarded_launch(codex_admitted):
    store, _, proposed = codex_admitted
    stop(store, proposed)
    before = dump(store)
    with pytest.raises(ControlStoreError):
        register(store, proposed)
    assert_pending(store)
    assert dump(store) == before


def test_guarded_launch_winning_first_prevents_preflight_stop(codex_admitted):
    store, _, proposed = codex_admitted
    register(store, proposed)
    before = dump(store)
    with pytest.raises(ControlStoreError):
        stop(store, proposed)
    assert stored_stop(store) is None and dump(store) == before


@pytest.mark.parametrize("state", ["completed", "cancelled", "abandoned"])
def test_generic_completion_cannot_terminalize_a_pending_preflight_stop(codex_admitted, state):
    store, _, proposed = codex_admitted
    stopped = stop(store, proposed)
    before = dump(store)
    with pytest.raises(ControlStoreError):
        store.complete_run(proposed.run_id, state=state, outcome="generic completion")
    assert stored_stop(store) == stopped and dump(store) == before
    assert_pending(store)


@pytest.mark.parametrize("method", ["release", "release_unlaunched", "release_retained"])
def test_preflight_stop_fences_generic_money_release_until_explicit_capture(codex_admitted, method):
    store, _, proposed = codex_admitted
    stop(store, proposed)
    before = dump(store)
    if method == "release_retained":
        result = store.release_retained(proposed.run_id, "recovery", store.now() + timedelta(minutes=1))
    else:
        result = getattr(store, method)(proposed.run_id, "recovery")
    assert result.disposition is AccountingDisposition.CONFLICT
    assert dump(store) == before
    assert_pending(store)


def test_pending_stop_capture_preserves_reserved_run_and_spawn_pending_provenance(codex_admitted):
    store, selection, proposed = codex_admitted
    stopped = stop(store, proposed)
    evidence = ReturnedReviewChild(proposed.review_child, str(proposed.run_id), OWNER, RuntimeName.CODEX, stopped.result, store.now())
    snapshot = current(store, selection)
    assert store.record_reservation_retained(proposed.run_id, "runtime preflight failed", returned_review=evidence)
    assert store.returned_review_child("o/r", proposed.run_id) is None
    assert stored_stop(store).result == evidence.result
    assert current(store, selection) == snapshot
    assert_pending(store)
    before = dump(store)
    assert store.record_reservation_retained(proposed.run_id, "runtime preflight failed", returned_review=evidence)
    assert dump(store) == before


def test_preflight_stop_receipt_write_failure_rolls_back_every_effect(codex_admitted):
    store, _, proposed = codex_admitted
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_stop BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-execution-preflight-stop' BEGIN SELECT RAISE(ABORT,'stop refused'); END")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError, match="stop refused"):
        stop(store, proposed)
    assert stored_stop(store) is None and dump(store) == before
    with store.transaction() as db:
        db.execute("DROP TRIGGER refuse_stop")
    assert stop(store, proposed) == stored_stop(store)


def test_terminal_preflight_capture_commits_zero_charge_and_canonical_raw_return_once(codex_admitted):
    store, selection, proposed = codex_admitted
    stopped = stop(store, proposed)
    evidence = ReturnedReviewChild(proposed.review_child, str(proposed.run_id), OWNER, RuntimeName.CODEX,
                                    stopped.result, store.now() + timedelta(seconds=10))
    snapshot = current(store, selection)
    settled = store.settle_review_preflight_stop(evidence)
    assert settled is not None and settled.disposition is AccountingDisposition.AVAILABLE and not settled.idempotent
    assert store.returned_review_child("o/r", proposed.run_id) == replace(evidence, returned_at=stopped.stopped_at)
    assert current(store, selection) == snapshot
    assert store.snapshot(proposed.scope).reserved.micros == 0
    assert store.snapshot(proposed.scope).committed.micros == 0
    with store.transaction() as db:
        assert tuple(db.execute("SELECT provider_receipt_id,amount_micros,basis FROM charges").fetchone()) == (
            "codex:preflight-stop:run-1", 0, "synthetic")
        assert tuple(db.execute("SELECT state,outcome,started_at FROM runs").fetchone()) == ("completed", "settled", None)
    before = dump(store)
    replay = store.settle_review_preflight_stop(replace(evidence, returned_at=store.now() + timedelta(seconds=20)))
    assert replay is not None and replay.idempotent and replay.disposition is AccountingDisposition.AVAILABLE
    assert dump(store) == before
    with pytest.raises(ControlStoreError):
        store.settle_review_preflight_stop(replace(evidence, result=replace(evidence.result, summary="changed")))
    assert dump(store) == before


@pytest.mark.parametrize("table", ["charges", "dedupe_keys"])
def test_terminal_stop_capture_rolls_back_charge_and_raw_output_together(codex_admitted, table):
    store, _, proposed = codex_admitted
    stopped = stop(store, proposed)
    evidence = ReturnedReviewChild(proposed.review_child, str(proposed.run_id), OWNER, RuntimeName.CODEX,
                                    stopped.result, store.now())
    condition = " WHEN NEW.namespace='review-execution-return'" if table == "dedupe_keys" else ""
    with store.transaction() as db:
        db.execute(f"CREATE TRIGGER refuse_terminal BEFORE INSERT ON {table}{condition} "
                   "BEGIN SELECT RAISE(ABORT,'terminal refused'); END")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError, match="terminal refused"):
        store.settle_review_preflight_stop(evidence)
    assert store.returned_review_child("o/r", proposed.run_id) is None
    assert dump(store) == before
    assert_pending(store)


def test_launched_runtime_unavailable_does_not_gain_zero_cost_preflight_authority(codex_admitted):
    store, _, proposed = codex_admitted
    register(store, proposed)
    evidence = ReturnedReviewChild(proposed.review_child, str(proposed.run_id), OWNER, RuntimeName.CODEX,
                                    failure(), store.now())
    before = dump(store)
    assert store.settle_review_preflight_stop(evidence) is None and dump(store) == before
    assert store.record_reservation_retained(proposed.run_id, "usage unavailable", returned_review=evidence)
    assert store.returned_review_child("o/r", proposed.run_id) == evidence
    assert stored_stop(store) is None and store.snapshot(proposed.scope).reserved.micros == 60
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT policy FROM reservation_recovery").fetchone()[0] == "reconcile_only"


def test_recovery_finishes_interrupted_preflight_stop_and_later_runtime_return_replays(codex_admitted):
    store, selection, proposed = codex_admitted
    stopped = stop(store, proposed)
    snapshot = current(store, selection)
    assert_pending(store)
    assert store.returned_review_child("o/r", proposed.run_id) is None
    recovery = CodexSpendReconciler(store, ())
    assert recovery.reconcile(proposed.run_id)
    canonical = ReturnedReviewChild(proposed.review_child, str(proposed.run_id), OWNER, RuntimeName.CODEX,
                                     stopped.result, stopped.stopped_at)
    assert store.returned_review_child("o/r", proposed.run_id) == canonical
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "synthetic")
        assert tuple(db.execute("SELECT state,outcome FROM runs").fetchone()) == ("completed", "settled")
    assert current(store, selection) == snapshot
    assert store.snapshot(proposed.scope).reserved.micros == 0
    before = dump(store)
    assert recovery.reconcile(proposed.run_id) and dump(store) == before
    late = replace(canonical, returned_at=stopped.stopped_at + timedelta(minutes=5))
    replay = store.settle_review_preflight_stop(late)
    assert replay is not None and replay.idempotent and replay.disposition is AccountingDisposition.AVAILABLE
    assert store.returned_review_child("o/r", proposed.run_id) == canonical and dump(store) == before


@pytest.mark.parametrize("api", ["settle", "settle_review_return"])
@pytest.mark.parametrize("amount", [0, 12])
def test_ordinary_settlement_cannot_bypass_atomic_preflight_stop_capture(codex_admitted, api, amount):
    store, _, proposed = codex_admitted
    stopped = stop(store, proposed)
    evidence = ReturnedReviewChild(proposed.review_child, str(proposed.run_id), OWNER, RuntimeName.CODEX,
                                    stopped.result, store.now())
    before = dump(store)
    result = (store.settle(settlement(proposed, amount=amount)) if api == "settle" else
              store.settle_review_return(settlement(proposed, amount=amount), evidence))
    assert result.disposition is AccountingDisposition.CONFLICT
    assert store.returned_review_child("o/r", proposed.run_id) is None and dump(store) == before
    assert_pending(store)


def test_terminal_stop_requires_the_run_and_charge_to_retain_the_same_receipt(codex_admitted):
    store, _, proposed = codex_admitted
    stopped = stop(store, proposed)
    evidence = ReturnedReviewChild(proposed.review_child, str(proposed.run_id), OWNER, RuntimeName.CODEX,
                                    stopped.result, stopped.stopped_at)
    settled = store.settle_review_preflight_stop(evidence)
    assert settled is not None and settled.disposition is AccountingDisposition.AVAILABLE
    with store.transaction() as db:
        db.execute("UPDATE runs SET provider_receipt_id='forged-provider-receipt'")
    before = dump(store)
    with pytest.raises(ControlStoreError):
        store.settle_review_preflight_stop(evidence)
    assert dump(store) == before
    with pytest.raises(ControlStoreError):
        CodexSpendReconciler(store, ()).reconcile(proposed.run_id)
    assert dump(store) == before


def test_real_codex_missing_binary_preflight_captures_stop_and_raw_result_offline(codex_planned, tmp_path):
    store, selection, child = codex_planned
    runner = CodexRunner(codex_bin=str(tmp_path / "missing-codex"))
    accounting = AccountingRuntime(runner, store, "o/r", "review", Money("USD", 5_000_000), None, None,
                                   "cfg", "codex", BudgetPolicy({("review", "REVIEW"): Money("USD", 2_000_000)}), _rates(), owner_id=OWNER)
    journal = JournalingRuntime(accounting, Journal(str(tmp_path / "journal.jsonl")), repo="o/r", role="review")
    journal.set_context("review", "REVIEW", 9, None, claim_target=-9,
                        workflow=child.round.binding.rejection, review_child=child)
    result = asyncio.run(journal.run(RuntimePlan("review", model="model-a", max_budget_usd=2)))
    assert result.is_error and result.status == "blocked" and result.exit_reason is ExitReason.RUNTIME_UNAVAILABLE, result
    with store.transaction() as db:
        run_id = RunId(db.execute("SELECT run_id FROM runs").fetchone()[0])
    stopped = stored_stop(store, str(run_id))
    assert stopped is not None and stopped.result.summary == result.summary
    raw = store.returned_review_child("o/r", run_id)
    assert raw is not None and raw.result == stopped.result and raw.child == child
    with store.transaction() as db:
        assert tuple(db.execute("SELECT state,outcome FROM runs").fetchone()) == ("completed", "settled")
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "settled"
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "synthetic")
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
