"""Offline accounting and journal execution for one admitted review child."""

import asyncio
from dataclasses import replace
import os

import pytest

from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.control_store import AccountingDisposition, ReservationResult
from issue_agent.adapters.journal import JournalingRuntime
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.contracts import Money, RunId
from issue_agent.domain.outcome import AgentResult, ExitReason, MAX_BUDGET_STATUS
from issue_agent.domain.pricing import BudgetPolicy
from tests.adapters.test_review_execution_admission import planned, refusal_receipt
from tests.adapters.test_review_execution_control import OWNER, rig
from tests.fakes.legacy_journal import Journal


class ChildRuntime:
    def __init__(self, store, result):
        self.store = store
        self.result = result
        self.called = False

    async def run(self, task):
        self.called = True
        process = ProcessSupervisor.process_identity(os.getpid())
        assert process is not None
        self.store.register_process_lease(
            RunId(task.env["ISSUE_AGENT_RUN_ID"]), expected_owner_id=OWNER,
            pid=process.pid, pgid=os.getpgrp(), process_start_ticks=process.start_ticks,
        )
        return self.result


def runtime(store, child, result, journal_path, *, repository_cap=5_000_000):
    inner = ChildRuntime(store, result)
    accounting = AccountingRuntime(
        inner, store, "o/r", "review", Money("USD", repository_cap), None, None,
        "cfg", "claude", BudgetPolicy({("review", "REVIEW"): Money("USD", 2_000_000)}),
        owner_id=OWNER,
    )
    journal = JournalingRuntime(accounting, Journal(str(journal_path)),
                                repo="o/r", role="review")
    journal.set_context("review", "REVIEW", 9, None, claim_target=-9,
                        workflow=child.round.binding.rejection, review_child=child)
    return journal, inner


@pytest.fixture
def planned_child(planned):
    store, _, _, child = planned
    return store, child


def test_final_budget_trip_records_refusal_and_never_launches_child(planned_child, tmp_path):
    store, child = planned_child
    journal, inner = runtime(store, child, AgentResult(False, status="reviewed"),
                             tmp_path / "journal.jsonl", repository_cap=1_999_999)

    result = asyncio.run(journal.run(RuntimePlan("review", model="model-a", max_budget_usd=2)))

    assert not inner.called
    assert result.status == MAX_BUDGET_STATUS and result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    refusal = refusal_receipt(store, child)
    assert refusal is not None and refusal.cap_micros == 1_999_999
    assert refusal.requested_micros == 2_000_000
    assert result.admission_budget_block is not None
    assert result.admission_budget_block.cap_micros == refusal.cap_micros
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0


def test_reconciled_first_trip_then_admission_leaves_no_refusal(planned_child, tmp_path):
    store, child = planned_child
    journal, inner = runtime(store, child, AgentResult(False, status="reviewed", cost_usd=1),
                             tmp_path / "journal.jsonl")
    attempts = 0

    def admit(proposed):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return ReservationResult(AccountingDisposition.EXHAUSTED, None,
                                     Money.zero("USD"), proposed.quote.maximum_charge,
                                     Money("USD", 1_999_999), "transient trip", "repository")
        return store.reserve(proposed)

    class Reconciler:
        def reconcile_stale_reservations(self):
            return 1

    journal.inner = replace(journal.inner, admit=admit, reservation_reconciler=Reconciler())
    journal.set_context("review", "REVIEW", 9, None, claim_target=-9,
                        workflow=child.round.binding.rejection, review_child=child)
    result = asyncio.run(journal.run(RuntimePlan("review", model="model-a", max_budget_usd=2)))

    assert attempts == 2 and inner.called and result.status == "reviewed"
    assert refusal_receipt(store, child) is None


def test_launched_review_result_settles_charge_and_retains_raw_return(planned_child, tmp_path):
    store, child = planned_child
    result = AgentResult(False, status="reviewed", summary="one finding", session_id="session-1",
                         cost_usd=1, structured={"findings": ["one"]})
    journal, inner = runtime(store, child, result, tmp_path / "journal.jsonl")

    returned = asyncio.run(journal.run(RuntimePlan("review", model="model-a", max_budget_usd=2)))

    assert inner.called and returned.status == "reviewed", returned.summary
    with store.transaction() as db:
        charge = db.execute("SELECT run_id,amount_micros FROM charges").fetchone()
        assert charge is not None and charge["amount_micros"] == 1_000_000
        run = db.execute("SELECT state,outcome FROM runs WHERE run_id=?", (charge["run_id"],)).fetchone()
        assert tuple(run) == ("completed", "settled")
    captured = store.returned_review_child("o/r", RunId(charge["run_id"]))
    assert captured is not None
    assert captured.child == child and captured.owner_id == OWNER
    assert captured.result.status == "reviewed" and captured.result.session_id == "session-1"
    assert captured.result.structured_json == '{"findings":["one"]}'


def test_launched_review_without_trusted_usage_retains_raw_output_and_reservation(planned_child, tmp_path):
    store, child = planned_child
    journal, inner = runtime(store, child, AgentResult(True, status="runtime_unavailable",
                                                       summary="transport failed",
                                                       exit_reason=ExitReason.RUNTIME_UNAVAILABLE),
                             tmp_path / "journal.jsonl")

    returned = asyncio.run(journal.run(RuntimePlan("review", model="model-a", max_budget_usd=2)))

    assert inner.called and returned.status == "runtime_unavailable", returned
    with store.transaction() as db:
        run = db.execute("SELECT run_id,state,outcome FROM runs").fetchone()
        assert tuple(run)[1:] == ("completed", "spend_unreconciled")
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "active"
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
    captured = store.returned_review_child("o/r", RunId(run["run_id"]))
    assert captured is not None and captured.result.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert captured.result.summary == "transport failed"


def test_invalid_provider_cost_still_retains_launched_review_output(planned_child, tmp_path):
    store, child = planned_child
    journal, inner = runtime(store, child, AgentResult(False, status="reviewed", summary="finding",
                                                       structured={"decision": "changes"}, cost_usd=float("nan")),
                             tmp_path / "journal.jsonl")

    with pytest.raises(ValueError, match="finite"):
        asyncio.run(journal.run(RuntimePlan("review", model="model-a", max_budget_usd=2)))

    assert inner.called
    with store.transaction() as db:
        run = db.execute("SELECT run_id,state,outcome FROM runs").fetchone()
        assert tuple(run)[1:] == ("completed", "spend_unreconciled")
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "active"
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
    captured = store.returned_review_child("o/r", RunId(run["run_id"]))
    assert captured is not None and captured.result.summary == "finding"
    assert captured.result.structured_json == '{"decision":"changes"}'
