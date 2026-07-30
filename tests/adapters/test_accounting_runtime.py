import asyncio
import os

import pytest

from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.codex_runner import (
    spend_evidence_path,
    write_spend_evidence,
)
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.event_log import EventLog
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.contracts import Money
from issue_agent.domain.events import Producer, SpendRecorded, decode_event_line
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.pricing import (
    BudgetPolicy,
    ModelRates,
    RateTable,
    UnknownModelPolicy,
)


def _policy(maximum=2_000_000):
    return BudgetPolicy({("fix", "RESOLVE"): Money("USD", maximum)})


def _rates():
    return RateTable(
        "test",
        "v1",
        "USD",
        {},
        UnknownModelPolicy.USE_FALLBACK,
        ModelRates(5_000_000, 30_000_000, 500_000, 5_000_000),
    )


class Runtime:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def run(self, _task):
        self.calls += 1
        return self.result


def _wrapper(tmp_path, runtime, cap=5_000_000, evidence_dir=""):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    wrapper = AccountingRuntime(
        runtime,
        store,
        "o/r",
        "fix",
        Money("USD", cap),
        None,
        None,
        "cfg",
        "codex",
        _policy(),
        _rates(),
        spend_evidence_dir=evidence_dir,
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1)
    return (
        wrapper,
        store,
    )


def test_paid_run_has_reservation_before_runtime_and_settles(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1.25,
            session_id="receipt",
            usage={"input_tokens": 100, "output_tokens": 10},
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    assert result.status == "ok"
    assert runtime.calls == 1
    blocked = asyncio.run(wrapper.run(RuntimePlan("more", max_budget_usd=4)))
    assert blocked.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1


def test_exhausted_cap_blocks_before_runtime(tmp_path) -> None:
    runtime = Runtime(AgentResult(False, status="ok"))
    wrapper, _store = _wrapper(tmp_path, runtime, cap=1_000_000)

    result = asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert runtime.calls == 0
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED


def test_disabled_caps_still_reserve_and_settle_subscription_run(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(False, status="ok", cost_usd=0, session_id="subscription")
    )
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    wrapper = AccountingRuntime(
        runtime,
        store,
        "o/r",
        "fix",
        None,
        None,
        None,
        "cfg",
        "claude",
        _policy(),
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1)

    result = asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert result.status == "ok"
    assert runtime.calls == 1
    assert store.active_reservation_runs() == ()


def test_subscription_usage_is_synthetically_priced_and_consumes_cap(
    tmp_path,
) -> None:
    class SubscriptionRuntime:
        def __init__(self):
            self.calls = 0

        async def run(self, _task):
            self.calls += 1
            return AgentResult(
                False,
                status="ok",
                cost_usd=0,
                session_id=f"subscription-{self.calls}",
                usage={"input_tokens": 1_000_000, "output_tokens": 100_000},
            )

    runtime = SubscriptionRuntime()
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    wrapper = AccountingRuntime(
        runtime,
        store,
        "o/r",
        "fix",
        Money("USD", 5_000_000),
        None,
        None,
        "cfg",
        "claude",
        _policy(),
        _rates(),
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1)

    first = asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )
    second = asyncio.run(
        wrapper.run(RuntimePlan("more", model="gpt-test", max_budget_usd=2))
    )

    assert first.status == "ok"
    assert second.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1
    with store._connect() as db:
        charge = db.execute(
            "SELECT amount_micros, basis, rate_card_id FROM charges"
        ).fetchone()
    assert tuple(charge) == (8_000_000, "synthetic", "test")


def test_runtime_exception_releases_reservation_without_daemon_restart(
    tmp_path,
) -> None:
    class Death:
        async def run(self, _task):
            raise RuntimeError("transport died")

    wrapper, store = _wrapper(tmp_path, Death())
    try:
        asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))
    except RuntimeError:
        pass
    assert store.active_reservation_runs() == ()
    with store._connect() as db:
        state, reason = db.execute(
            "SELECT state, release_reason FROM budget_reservations"
        ).fetchone()
    assert (state, reason) == (
        "released",
        "runtime_exception_spend_unknown",
    )


def test_result_normalization_failure_releases_reservation(tmp_path) -> None:
    wrapper, store = _wrapper(
        tmp_path,
        Runtime(
            AgentResult(
                False,
                status="ok",
                usage={"input_tokens": "malformed"},
            )
        ),
    )

    with pytest.raises(ValueError):
        asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert store.active_reservation_runs() == ()


def test_journal_supplied_run_identity_is_the_reservation_identity(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(False, status="ok", cost_usd=1, session_id="receipt")
    )
    wrapper, store = _wrapper(tmp_path, runtime)
    event_path = tmp_path / "events.jsonl"
    events = EventLog(
        str(event_path),
        Producer("deployment", "o/r", "fix", 123, "boot"),
    )
    store.bind_event_outbox(events.prepare, events.append_committed)
    wrapper.set_run_identity("journal-run", "journal-leg")

    asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    emitted = tuple(
        decode_event_line(line) for line in event_path.read_text().splitlines()
    )
    spend = next(
        event for event in emitted if isinstance(event.payload, SpendRecorded)
    )
    assert spend.correlation.run_id == "journal-run"


def test_successful_settlement_removes_crash_recovery_evidence(tmp_path) -> None:
    evidence_dir = str(tmp_path / "spend-evidence")
    wrapper, _store = _wrapper(
        tmp_path,
        Runtime(AgentResult(False, status="ok", session_id="receipt")),
        evidence_dir=evidence_dir,
    )
    wrapper.set_run_identity("known-run", "leg")
    write_spend_evidence(evidence_dir, "known-run", {"session_id": "receipt"})

    asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert not os.path.exists(spend_evidence_path(evidence_dir, "known-run"))
