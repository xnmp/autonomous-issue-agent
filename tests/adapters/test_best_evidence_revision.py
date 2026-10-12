"""Behavioral regressions from the third independent #301 review."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from itertools import permutations
from pathlib import Path
import sys

import pytest

from issue_agent.adapters.codex_child_usage import CodexChildUsageMeter
from issue_agent.adapters.control_store import AccountingDisposition, SQLiteControlStore
from issue_agent.adapters.codex_runner import spend_evidence_path, write_spend_evidence
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.contracts import ChargeOrigin, Money, PricingBasis, SettlementRequest
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.pricing import BudgetPolicy
from issue_agent.domain.run_usage import price_run_usage
from tests.adapters import test_native_usage_accounting as native
from tests.adapters.test_accounting_runtime import Runtime, _wrapper
from tests.adapters.test_codex_child_usage import ROOT, CHILD, START, RATES, event, metadata, path, write
from tests.adapters.test_settlement_regressions import counter, receipt, child_rollout


def priced_receipt(sid=ROOT):
    record = receipt(sid, "priced", 100)
    record["payload"]["cost_usd"] = .0009
    return record


@pytest.mark.parametrize("order", tuple(permutations(range(3))))
def test_mixed_request_evidence_prices_each_receipt_and_only_the_counter_remainder(tmp_path, order):
    home = tmp_path / "codex"
    records = [counter(250), priced_receipt(), receipt(ROOT, "tokens", 100)]
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
        *(records[i] for i in order), records[1]])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert price_run_usage(observed, RATES) == Money("USD", 1200)


@pytest.mark.parametrize("missing_tokens", [False, True])
def test_usable_provider_dollars_survive_an_unusaged_completed_request_and_journal_gap(tmp_path, missing_tokens):
    home = tmp_path / "codex"
    record = priced_receipt()
    if missing_tokens:
        del record["payload"]["usage"]
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
        record, event("event_msg", {"type": "task_complete", "turn_id": "t"}),
        event("event_msg", {"type": "task_complete", "turn_id": "missing"})])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status="pushed", run_usage=observed)))
    wrapper = replace(wrapper, rate_table=RATES)
    wrapper.set_context("fix", "RESOLVE", 1, 1)
    asyncio.run(wrapper.run(RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=2)))
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (900, "estimate")


def test_mixed_request_evidence_stops_live_execution_at_the_same_settled_amount(tmp_path, monkeypatch):
    records = child_rollout(tmp_path, [counter(200), priced_receipt(CHILD), receipt(CHILD, "tokens", 100)])
    monkeypatch.setattr(native, "child_records", lambda *_args, **_kwargs: records)
    directory = tmp_path / "evidence"
    wrapper, store = _wrapper(tmp_path, native.native_runner(tmp_path, native.native_executable(tmp_path), directory),
                              evidence_dir=str(directory))
    wrapper = replace(wrapper, rate_table=RATES,
                      budget_policy=BudgetPolicy({("fix", "RESOLVE"): Money("USD", 2000)}))
    wrapper.set_context("fix", "RESOLVE", 1, 1)
    result = asyncio.run(wrapper.run(RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=.002,
                                                 env={"CODEX_HOME": str(tmp_path / "codex")})))
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert not (tmp_path / "finished").exists()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (2250, "estimate")
    assert store.active_reservation_runs() == ()


@pytest.mark.parametrize("normal_basis", [PricingBasis.ESTIMATE, PricingBasis.CONFIGURED_RATE_CARD, PricingBasis.PROVIDER_REPORTED])
@pytest.mark.parametrize("recovery_basis", [PricingBasis.ESTIMATE, PricingBasis.RECONCILED])
@pytest.mark.parametrize("same_receipt", [False, True])
def test_normal_estimated_return_is_final_even_after_restart_and_stale_replays(tmp_path, normal_basis, recovery_basis, same_receipt):
    store, scope, run = native._reserved_store(tmp_path, runtime="codex")
    native._end_run_at(store, run, native.ENDED)
    earlier = SettlementRequest(run, Money("USD", 1_000_000), recovery_basis,
                                f"codex-recovered:{ROOT}:{run}", None, "card", "v1",
                                origin=ChargeOrigin.RECOVERY_ESTIMATE)
    normal = replace(earlier, amount=Money("USD", 1_250_000), basis=normal_basis,
                     provider_receipt_id=earlier.provider_receipt_id if same_receipt else f"codex:{ROOT}:{run}",
                     origin=ChargeOrigin.NORMAL_RETURN)
    assert store.settle(earlier).disposition is AccountingDisposition.AVAILABLE
    assert store.settle(normal).disposition is AccountingDisposition.AVAILABLE
    restarted = SQLiteControlStore(store.path)
    restarted.initialize()
    assert restarted.settle(normal).idempotent
    assert restarted.settle(replace(earlier, provider_receipt_id=f"earlier-estimate:{run}")).disposition is AccountingDisposition.CONFLICT
    assert restarted.settle(earlier).disposition is AccountingDisposition.CONFLICT
    assert restarted.settle(replace(earlier, provider_receipt_id=f"old-normal:{run}",
                                   origin=ChargeOrigin.NORMAL_RETURN)).disposition is AccountingDisposition.CONFLICT
    assert restarted.snapshot(scope).settled == Money("USD", 1_250_000)


def test_provider_cost_does_not_require_a_child_token_price_or_model(tmp_path):
    home = tmp_path / "codex"
    from tests.adapters.test_codex_child_usage import spawn
    write(home, ROOT, [metadata(ROOT, tmp_path), *spawn()])
    write(home, CHILD, [metadata(CHILD, tmp_path, ROOT), priced_receipt(CHILD)])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert price_run_usage(observed, RATES) == Money("USD", 900)


@pytest.mark.parametrize("kind", ["receipt", "counter", "partial"])
def test_terminal_recovery_parses_complete_final_records_without_newline(tmp_path, kind):
    store, scope, run = native._reserved_store(tmp_path, runtime="codex")
    home, evidence = native.recovery_files(tmp_path)
    path(home, CHILD).unlink()
    record = counter(100) if kind == "counter" else receipt(ROOT, "paid", 100)
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}), record])
    rollout = path(home, ROOT)
    raw = rollout.read_text().rstrip("\n")
    rollout.write_text(raw[:-2] if kind == "partial" else raw)
    directory = tmp_path / "evidence"
    write_spend_evidence(str(directory), str(run), evidence)
    native._end_run_at(store, run, native.ENDED)
    reconciler = native.CodexSpendReconciler(store, (str(directory),))
    assert reconciler.reconcile(run)
    assert store.snapshot(scope).settled == Money("USD", 0 if kind == "partial" else 200)
    assert not Path(spend_evidence_path(str(directory), str(run))).exists()
    reconciler.reconcile(run)
    assert store.snapshot(scope).settled == Money("USD", 0 if kind == "partial" else 200)


@pytest.mark.parametrize("linked_prior_counter", [False, True])
def test_prelaunch_receipt_counters_cannot_reprice_a_resumed_parent_snapshot(tmp_path, linked_prior_counter):
    from tests.adapters.test_codex_child_usage import NOW
    home = tmp_path / "codex"
    old = receipt(ROOT, "prior", 1000, 1000 if linked_prior_counter else None)
    old["timestamp"] = (NOW - timedelta(seconds=10)).isoformat()
    write(home, ROOT, [metadata(ROOT, tmp_path), old,
        event("turn_context", {"turn_id": "t", "model": "sol"}), receipt(ROOT, "new", 100, 1100)])
    meter = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path))
    meter.seed_baseline(ROOT, {"input_tokens": 500, "output_tokens": 0, "source": "rollout"})
    observed = meter.sample(ROOT, "sol", {"input_tokens": 600, "output_tokens": 0, "source": "rollout"}, final=True)
    assert price_run_usage(observed, RATES) == Money("USD", 200)


def test_delayed_recovery_uses_the_completed_invocation_window_for_discovery(tmp_path, monkeypatch):
    import issue_agent.adapters.codex_child_usage as metering
    from tests.adapters.test_codex_child_usage import NOW
    from datetime import datetime
    store, scope, run = native._reserved_store(tmp_path, runtime="codex")
    home, evidence = native.recovery_files(tmp_path)
    path(home, CHILD).unlink()
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
                       priced_receipt()])
    directory = tmp_path / "evidence"
    write_spend_evidence(str(directory), str(run), evidence)
    native._end_run_at(store, run, NOW + timedelta(seconds=1))

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(days=8)

    monkeypatch.setattr(metering, "datetime", Later)
    assert native.CodexSpendReconciler(store, (str(directory),)).reconcile(run)
    assert store.snapshot(scope).settled == Money("USD", 900)
    assert not Path(spend_evidence_path(str(directory), str(run))).exists()


def test_recovery_keeps_provider_dollars_without_a_frozen_token_card(tmp_path):
    store, scope, run = native._reserved_store(tmp_path, runtime="codex")
    home, evidence = native.recovery_files(tmp_path)
    evidence["native_rate_table"] = None
    path(home, CHILD).unlink()
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
                       priced_receipt()])
    directory = tmp_path / "evidence"
    write_spend_evidence(str(directory), str(run), evidence)
    native._end_run_at(store, run, native.ENDED)
    assert native.CodexSpendReconciler(store, (str(directory),)).reconcile(run)
    assert store.snapshot(scope).settled == Money("USD", 900)


def test_normal_return_keeps_provider_dollars_when_caps_and_token_card_are_absent(tmp_path):
    home = tmp_path / "codex"
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
                       priced_receipt()])
    observed = CodexChildUsageMeter(str(home), START, None, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status="pushed", run_usage=observed)))
    wrapper = replace(wrapper, rate_table=None, repository_cap=None, global_cap=None, issue_cap=None)
    wrapper.set_context("fix", "RESOLVE", 1, 1)
    result = asyncio.run(wrapper.run(RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=2)))
    assert result.status == "pushed", result.summary
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (900, "provider_reported")


def test_normal_return_uses_the_same_frozen_mixed_price_as_the_live_meter_without_wrapper_rates(tmp_path):
    home = tmp_path / "codex"
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
                       counter(200), priced_receipt()])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert price_run_usage(observed, RATES) == Money("USD", 1100)
    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status="pushed", run_usage=observed)))
    wrapper = replace(wrapper, rate_table=None, repository_cap=None, global_cap=None, issue_cap=None)
    wrapper.set_context("fix", "RESOLVE", 1, 1)
    asyncio.run(wrapper.run(RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=2)))
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis,rate_card_id FROM charges").fetchone()) == (1100, "estimate", "card")


@pytest.mark.parametrize("cap,expected", [(.001, ExitReason.OK), (.0009001, ExitReason.BUDGET_EXHAUSTED)])
def test_legacy_runner_final_cap_uses_provider_dollars_instead_of_parallel_token_estimate(tmp_path, cap, expected):
    record = priced_receipt()
    record["payload"]["usage"]["input_tokens"] = 1000
    records = [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
               counter(1000), record]
    script = tmp_path / "codex"
    script.write_text(f"#!{sys.executable}\n" + f'''
import json, os, pathlib, sys
from datetime import datetime, timezone
now = datetime.now(timezone.utc)
records = {records!r}
for record in records:
    record["timestamp"] = now.isoformat()
home = pathlib.Path(os.environ["CODEX_HOME"]) / "sessions" / now.strftime("%Y/%m/%d")
home.mkdir(parents=True, exist_ok=True)
(home / "rollout-now-{ROOT}.jsonl").write_text("".join(json.dumps(record) + "\\n" for record in records))
print("session id: {ROOT}", file=sys.stderr, flush=True)
pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps({{"status": "pushed", "summary": "done", "needs": ""}}))
''')
    script.chmod(0o700)
    runner = replace(native.native_runner(tmp_path, script, tmp_path / "evidence"), rate_table=None, wall_clock=lambda: 0)
    result = asyncio.run(runner.run(RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=cap,
                                                env={"CODEX_HOME": str(tmp_path / "home")})))
    assert result.exit_reason is expected
    assert result.cost_usd == pytest.approx(.0009)
