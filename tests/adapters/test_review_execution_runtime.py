"""Offline accounting and journal execution for one admitted review child."""

import asyncio
import os

import pytest

from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.journal import JournalingRuntime
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.contracts import Money, RunId
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.pricing import BudgetPolicy
from tests.adapters.test_review_execution_admission import planned
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


def runtime(store, child, result, journal_path):
    inner = ChildRuntime(store, result)
    accounting = AccountingRuntime(
        inner, store, "o/r", "review", Money("USD", 5_000_000), None, None,
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
