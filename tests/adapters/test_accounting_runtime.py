import asyncio
import dataclasses
import json
import os

import pytest

from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.codex_runner import (
    spend_evidence_path,
    write_spend_evidence,
)
from issue_agent.adapters.control_store import Lease, SQLiteControlStore
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.contracts import Money, RunId
from issue_agent.domain.outcome import AgentResult, DAILY_HEADROOM_STATUS, ExitReason, MAX_BUDGET_STATUS
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


def test_unpinned_codex_run_without_reported_model_uses_reserved_fallback(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1.25,
            session_id="resume-receipt",
            usage={
                "input_tokens": 100,
                "output_tokens": 10,
                "source": "rollout",
            },
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)

    result = asyncio.run(
        wrapper.run(RuntimePlan("resume", max_budget_usd=2))
    )

    assert result.status == "ok"
    assert store.active_reservation_runs() == ()
    with store._connect() as db:
        reservation_model = db.execute(
            "SELECT model FROM budget_reservations"
        ).fetchone()[0]
        amount, basis = db.execute(
            "SELECT amount_micros, basis FROM charges"
        ).fetchone()
    assert reservation_model == "<runtime-default>"
    assert (amount, basis) == (800, "estimate")


def test_global_only_cap_keeps_workload_repository(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1,
            session_id="global-receipt",
            usage={"input_tokens": 100, "output_tokens": 10},
        )
    )
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    wrapper = AccountingRuntime(
        runtime,
        store,
        "o/r",
        "fix",
        None,
        Money("USD", 5_000_000),
        None,
        "cfg",
        "codex",
        _policy(),
        _rates(),
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1)

    result = asyncio.run(
        wrapper.run(
            RuntimePlan("work", model="gpt-test", max_budget_usd=2)
        )
    )

    assert result.status == "ok"
    with store._connect() as db:
        attribution = db.execute(
            "SELECT repository, workload_repository "
            "FROM budget_reservations"
        ).fetchone()
    assert tuple(attribution) == ("*", "o/r")


def test_production_runtime_binds_paid_run_to_dispatch_claim(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1,
            session_id="receipt",
            usage={"input_tokens": 10, "output_tokens": 5},
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)
    wrapper = dataclasses.replace(
        wrapper,
        owner_id="o/r:fix:deployment",
        require_dispatch_claim=True,
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1)
    assert store.claim_dispatch(
        "o/r",
        "fix",
        1,
        "RESOLVE",
        "o/r:fix:deployment",
    )

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    assert result.status == "ok"
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM dispatch_claim_runs "
            "WHERE repository='o/r' AND role='fix' AND target='issue:1'"
        ).fetchone()[0] == 1


def test_production_runtime_releases_admission_when_claim_is_missing(
    tmp_path,
) -> None:
    runtime = Runtime(AgentResult(False, status="must-not-run"))
    wrapper, store = _wrapper(tmp_path, runtime)
    wrapper = dataclasses.replace(
        wrapper,
        owner_id="o/r:fix:deployment",
        require_dispatch_claim=True,
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1)

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    assert result.status == "dispatch_claim_unavailable"
    assert runtime.calls == 0
    assert store.active_reservation_runs() == ()


def test_linked_review_separates_issue_claim_from_pr_retry_key(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="reviewed",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)
    owner = "o/r:review:deployment"
    wrapper = dataclasses.replace(
        wrapper,
        role="review",
        owner_id=owner,
        require_dispatch_claim=True,
        budget_policy=BudgetPolicy(
            {("review", "REVIEW"): Money("USD", 2_000_000)}
        ),
    )
    assert store.claim_dispatch(
        "o/r",
        "review",
        9,
        "REVIEW",
        owner,
    )
    wrapper.set_context(
        "review",
        "REVIEW",
        42,
        9,
        claim_target=9,
    )

    result = asyncio.run(
        wrapper.run(
            RuntimePlan("review", model="gpt-test", max_budget_usd=2)
        )
    )

    assert result.status == "reviewed"
    with store._connect() as db:
        payload = json.loads(
            db.execute("SELECT dispatch_id FROM runs").fetchone()[0]
        )
    assert payload["claim_target"] == 9


def test_missing_issue_still_blocks_issue_scoped_fix_work(tmp_path) -> None:
    runtime = Runtime(AgentResult(False, status="should-not-run"))
    wrapper, _store = _wrapper(tmp_path, runtime)
    wrapper = dataclasses.replace(
        wrapper,
        issue_cap=Money("USD", 25_000_000),
    )
    wrapper.set_context("fix", "RESOLVE", 1, None)

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", max_budget_usd=2))
    )

    assert result.summary == (
        "paid admission blocked: issue spend scope is unavailable"
    )
    assert runtime.calls == 0


def test_resumed_session_uses_distinct_per_leg_settlement_receipts(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=0.1,
            session_id="resumed-session",
            usage={
                "input_tokens": 100,
                "output_tokens": 10,
                "source": "rollout",
            },
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
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")


def test_stderr_codex_usage_cannot_release_a_paid_reservation(
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
                "source": "stderr",
            },
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)

    asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")


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


def test_disabled_caps_still_price_observed_subscription_stream_usage(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="stream-only",
            cost_usd=0,
            usage=None,
            stream_usage={
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
            },
        )
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
        _rates(),
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1)

    result = asyncio.run(
        wrapper.run(
            RuntimePlan("work", model="gpt-test", max_budget_usd=2)
        )
    )

    assert result.status == "stream-only"
    with store._connect() as db:
        amount, basis, usage_json, rate_card = db.execute(
            "SELECT amount_micros, basis, usage_json, rate_card_id "
            "FROM charges"
        ).fetchone()
    assert amount == 8_000_000
    assert basis == "estimate"
    assert json.loads(usage_json)["input_tokens"] == 1_000_000
    assert rate_card == "test"


def test_disabled_caps_honor_explicit_unpriced_codex_opt_out(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(False, status="untracked", cost_usd=0, usage=None)
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
        "codex",
        _policy(),
        None,
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1)

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", max_budget_usd=2))
    )

    assert result.status == "untracked"
    assert runtime.calls == 1
    assert store.active_reservation_runs() == ()
    with store._connect() as db:
        amount, basis = db.execute(
            "SELECT amount_micros, basis FROM charges"
        ).fetchone()
    assert (amount, basis) == (0, "estimate")


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
    assert tuple(charge) == (8_000_000, "estimate", "test")


def test_armed_subscription_cap_prices_stream_usage_when_final_usage_is_missing(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="no-final-usage",
            cost_usd=0,
            session_id="stream-observed",
            usage=None,
            stream_usage={
                "input_tokens": 1_100_000,
                "output_tokens": 0,
            },
        )
    )
    wrapper, store = _wrapper(
        tmp_path,
        runtime,
        cap=2_000_000,
        runtime_name="claude",
    )

    first = asyncio.run(
        wrapper.run(
            RuntimePlan("work", model="gpt-test", max_budget_usd=2)
        )
    )
    second = asyncio.run(
        wrapper.run(
            RuntimePlan("more", model="gpt-test", max_budget_usd=2)
        )
    )

    assert first.status == "no-final-usage"
    assert second.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1
    with store._connect() as db:
        amount, basis, usage_json = db.execute(
            "SELECT amount_micros, basis, usage_json FROM charges"
        ).fetchone()
    assert amount == 5_500_000
    assert basis == "estimate"
    assert json.loads(usage_json)["input_tokens"] == 1_100_000


def test_armed_non_codex_run_without_any_usage_settles_zero_estimate(
    tmp_path,
) -> None:
    wrapper, store = _wrapper(
        tmp_path,
        Runtime(
            AgentResult(
                False,
                status="usage-blind",
                cost_usd=0,
                usage=None,
                stream_usage=None,
            )
        ),
        runtime_name="claude",
    )

    result = asyncio.run(
        wrapper.run(
            RuntimePlan("work", model="gpt-test", max_budget_usd=2)
        )
    )

    assert result.status == "usage-blind"
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")


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
        "runtime_exception_before_launch",
    )

    # A machine failure is no attempt: the ended run carries no leg receipt.
    assert [run.receipt for run in store.leg_runs("o/r", (1,))] == [None]


def test_malformed_usage_settles_zero_estimate_without_changing_work_result(
    tmp_path,
) -> None:
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

    result = asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))
    assert result.status == "ok"
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")


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
        "runtime_exception_before_launch",
    )

    # A machine failure is no attempt: the ended run carries no leg receipt.
    assert [run.receipt for run in store.leg_runs("o/r", (1,))] == [None]


def test_runtime_exception_after_child_registration_retains_reservation(
    tmp_path,
) -> None:
    class Death:
        store: SQLiteControlStore
        run_id: RunId

        async def run(self, task):
            run_id = RunId(task.env["ISSUE_AGENT_RUN_ID"])
            self.run_id = run_id
            with self.store._connect() as db:
                dispatch_id, owner_id = db.execute(
                    "SELECT dispatch_id, owner_id FROM runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            self.store.register_lease(
                Lease(
                    run_id,
                    dispatch_id,
                    owner_id,
                    os.getpid(),
                    os.getpgid(os.getpid()),
                    self.store._now(),
                    1,
                )
            )
            raise RuntimeError("transport died after provider launch")

    runtime = Death()
    wrapper, store = _wrapper(tmp_path, runtime, runtime_name="claude")
    runtime.store = store

    with pytest.raises(RuntimeError, match="after provider launch"):
        asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert store.active_reservation_runs() == (runtime.run_id,)
    with store._connect() as db:
        policy, run_state, outcome, reservation_state = db.execute(
            "SELECT rr.policy, r.state, r.outcome, b.state "
            "FROM reservation_recovery rr JOIN runs r ON r.run_id=rr.run_id "
            "JOIN budget_reservations b ON b.run_id=rr.run_id"
        ).fetchone()
    assert (policy, run_state, outcome, reservation_state) == (
        "reconcile_only",
        "completed",
        "spend_unreconciled",
        "active",
    )


def test_journal_supplied_run_identity_is_the_reservation_identity(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(False, status="ok", cost_usd=1, session_id="receipt")
    )
    wrapper, store = _wrapper(tmp_path, runtime)
    wrapper.set_run_identity("journal-run", "journal-leg")

    asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    with store.transaction() as db:
        assert [tuple(row) for row in db.execute("SELECT run_id,state FROM runs")] == [
            ("journal-run", "completed")]
        assert [row[0] for row in db.execute("SELECT run_id FROM charges")] == ["journal-run"]


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


@pytest.mark.parametrize("cap_kind,status", [
    ("repository", DAILY_HEADROOM_STATUS), ("global", DAILY_HEADROOM_STATUS), ("issue", MAX_BUDGET_STATUS),
])
def test_fix_admission_refusal_is_a_daily_wait_only_for_daily_caps(tmp_path, cap_kind, status):
    # Decision #7: daily/global windows refill with time, so their refusal is a
    # label-free wait; the lifetime issue cap needs a human decision. Neither
    # opens a paid run; the zero-cost refused row spaces the retry (#262).
    runtime = Runtime(AgentResult(False, status="ok", cost_usd=.01))
    wrapper, store = _wrapper(tmp_path, runtime)
    wrapper = dataclasses.replace(wrapper, repository_cap=Money("USD", 0) if cap_kind == "repository" else None,
        global_cap=Money("USD", 0) if cap_kind == "global" else None,
        issue_cap=Money("USD", 0) if cap_kind == "issue" else None)
    wrapper.set_context("fix", "RESOLVE", 1, 1)
    result = asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))
    assert result.is_error and result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert (result.status, result.envelope_subtype) == (status, status)
    assert runtime.calls == 0
    with store.transaction() as db:
        assert [tuple(row) for row in db.execute("SELECT state, outcome FROM runs")] == [("cancelled", "admission_refused")]
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
    assert [run.receipt for run in store.leg_runs("o/r", (1,))] == [None]


@pytest.mark.parametrize("invalid", [None, True, -1, 1.5, "12", 10**100])
def test_malformed_primary_usage_preserves_valid_stream_estimate(tmp_path, invalid):
    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status="done",
        usage={"input_tokens": invalid, "output_tokens": 1},
        stream_usage={"input_tokens": 100, "output_tokens": 10})), runtime_name="claude")
    result = asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))
    assert result.status == "done" and store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (800, "estimate")


def test_valid_provider_dollars_survive_malformed_token_telemetry(tmp_path):
    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status="done", cost_usd=1.25,
        usage={"input_tokens": "bad"})), runtime_name="claude")
    asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (1_250_000, "provider_reported")


@pytest.mark.parametrize("invalid_cost", [float("nan"), float("inf"), -1, 10**100])
def test_unusable_provider_dollars_preserve_valid_stream_estimate(tmp_path, invalid_cost):
    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status="done", cost_usd=invalid_cost,
        stream_usage={"input_tokens": 100, "output_tokens": 10})), runtime_name="claude")
    asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (800, "estimate")


def test_a_completed_leg_settles_and_the_daily_cap_gates_with_no_event_log_or_outbox(
    tmp_path,
) -> None:
    runtime = Runtime(AgentResult(False, status="ok", cost_usd=1.25, session_id="receipt"))
    wrapper, store = _wrapper(tmp_path, runtime)

    settled = asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))
    refused = asyncio.run(wrapper.run(RuntimePlan("more", model="gpt-test", max_budget_usd=4)))

    assert settled.status == "ok"
    assert refused.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1
    with store.transaction() as db:
        assert [row[0] for row in db.execute("SELECT amount_micros FROM charges")] == [1_250_000]
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('event_outbox','world_observations')"
        ).fetchall() == []
    assert sorted(path.name for path in tmp_path.iterdir() if path.suffix == ".jsonl") == []
