import asyncio
import base64
import dataclasses
import json
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


def _wrapper(
    tmp_path,
    runtime,
    cap=5_000_000,
    evidence_dir="",
    runtime_name="codex",
):
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
        runtime_name,
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


def test_paid_run_carries_portable_ancestor_authority_bundle(tmp_path) -> None:
    class CapturingRuntime(Runtime):
        async def run(self, task):
            self.task = task
            return await super().run(task)

    runtime = CapturingRuntime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1,
            session_id="receipt",
            usage={"input_tokens": 100, "output_tokens": 10},
        )
    )
    wrapper, _ = _wrapper(tmp_path, runtime)
    wrapper = dataclasses.replace(wrapper, github_host="github.example")
    wrapper.set_context("fix", "RESOLVE", 1, 1)

    asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    encoded = runtime.task.env["ISSUE_AGENT_AUTHORITY_BUNDLE"]
    authority = json.loads(base64.urlsafe_b64decode(encoded).decode())
    assert authority["ISSUE_AGENT_GITHUB_HOST"] == "github.example"
    assert authority["ISSUE_AGENT_REPOSITORY"] == "o/r"
    assert authority["ISSUE_AGENT_CONTROL_DB"].endswith("control.db")
    assert authority["ISSUE_AGENT_RUN_ID"]


def test_resumed_session_uses_distinct_per_leg_settlement_receipts(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=0.1,
            session_id="resumed-session",
            usage={"input_tokens": 100, "output_tokens": 10},
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)

    asyncio.run(
        wrapper.run(RuntimePlan("first", model="gpt-test", max_budget_usd=2))
    )
    asyncio.run(
        wrapper.run(RuntimePlan("second", model="gpt-test", max_budget_usd=2))
    )

    with store._connect() as db:
        receipts = tuple(
            row[0]
            for row in db.execute(
                "SELECT provider_receipt_id FROM charges ORDER BY charge_id"
            )
        )
    assert len(receipts) == 2
    assert len(set(receipts)) == 2
    assert all("resumed-session" in receipt for receipt in receipts)
    assert store.active_reservation_runs() == ()


def test_codex_missing_usage_never_settles_productive_run_as_free(
    tmp_path,
) -> None:
    wrapper, store = _wrapper(
        tmp_path,
        Runtime(
            AgentResult(
                False,
                status="pushed",
                cost_usd=0,
                session_id="usage-blind",
                usage=None,
            )
        ),
    )

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    assert result.status == "pushed"
    assert len(store.active_reservation_runs()) == 1
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0


def test_total_only_codex_usage_cannot_turn_nonzero_spend_into_zero(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1.25,
            session_id="total-only-receipt",
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "total_tokens": 125_000,
            },
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)

    asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    with store._connect() as db:
        charge = db.execute(
            "SELECT amount_micros, basis, usage_json, rate_card_id, "
            "rate_card_version FROM charges"
        ).fetchone()
    assert tuple(charge) == (
        1_250_000,
        "configured_rate_card",
        None,
        "test",
        "v1",
    )


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


def test_codex_runtime_exception_releases_after_completed_teardown(
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


def test_codex_result_normalization_failure_releases_reservation(tmp_path) -> None:
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


def test_forced_codex_shutdown_intent_preserves_reservation_for_recovery(
    tmp_path,
) -> None:
    class Death:
        def __init__(self):
            self.prepare_shutdown = lambda: None

        async def run(self, _task):
            self.prepare_shutdown()
            raise RuntimeError("killed")

    runtime = Death()
    wrapper, store = _wrapper(tmp_path, runtime)
    runtime.prepare_shutdown = lambda: store.record_owned_cancellation_intents(
        "o/r:fix", "shutdown_deadline_spend_unknown"
    )

    with pytest.raises(RuntimeError, match="killed"):
        asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert len(store.active_reservation_runs()) == 1


def test_non_codex_runtime_exception_releases_after_completed_teardown(
    tmp_path,
) -> None:
    class Death:
        async def run(self, _task):
            raise RuntimeError("transport died")

    wrapper, store = _wrapper(tmp_path, Death(), runtime_name="claude")

    with pytest.raises(RuntimeError, match="transport died"):
        asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert store.active_reservation_runs() == ()
    with store._connect() as db:
        state, reason = db.execute(
            "SELECT state, release_reason FROM budget_reservations"
        ).fetchone()
    assert (state, reason) == (
        "released",
        "runtime_exception_spend_unknown",
    )


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
        Runtime(
            AgentResult(
                False,
                status="ok",
                cost_usd=1,
                session_id="receipt",
            )
        ),
        evidence_dir=evidence_dir,
    )
    wrapper.set_run_identity("known-run", "leg")
    write_spend_evidence(evidence_dir, "known-run", {"session_id": "receipt"})

    asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert not os.path.exists(spend_evidence_path(evidence_dir, "known-run"))


def test_concurrent_review_validators_share_context_without_false_dedup(
    tmp_path,
) -> None:
    class ConcurrentRuntime:
        def __init__(self):
            self.calls = 0
            self.ready = asyncio.Event()

        async def run(self, _task):
            self.calls += 1
            call = self.calls
            if call == 2:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), timeout=1)
            return AgentResult(
                False,
                status="ok",
                cost_usd=1,
                session_id=f"validator-{call}",
            )

    runtime = ConcurrentRuntime()
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()

    def validator(run_id: str, leg_id: str) -> AccountingRuntime:
        wrapper = AccountingRuntime(
            runtime,
            store,
            "o/r",
            "review",
            Money("USD", 5_000_000),
            None,
            None,
            "cfg",
            "claude",
            BudgetPolicy(
                {
                    ("review", "VALIDATE_SECURITY"): Money(
                        "USD", 2_000_000
                    )
                }
            ),
        )
        wrapper.set_context("review", "VALIDATE_SECURITY", 7, 1)
        wrapper.set_run_identity(run_id, leg_id)
        return wrapper

    async def run_both():
        return await asyncio.gather(
            validator("run-a", "finding-a").run(
                RuntimePlan("a", max_budget_usd=2)
            ),
            validator("run-b", "finding-b").run(
                RuntimePlan("b", max_budget_usd=2)
            ),
        )

    results = asyncio.run(run_both())

    assert runtime.calls == 2
    assert [result.status for result in results] == ["ok", "ok"]
    assert store.active_reservation_runs() == ()
