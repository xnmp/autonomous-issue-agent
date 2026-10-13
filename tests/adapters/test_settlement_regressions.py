"""Behavioral reproductions from the second independent #301 review."""

from dataclasses import replace
import asyncio
from itertools import permutations
from datetime import timedelta
import pytest
from tests.adapters.test_codex_child_usage import ROOT, CHILD, START, RATES, event, metadata, spawn, write
from tests.adapters.test_accounting_runtime import Runtime, _db, _lease, _run, _wrapper
from tests.adapters import test_native_usage_accounting as native
from issue_agent.adapters.codex_child_usage import CodexChildUsageMeter
from issue_agent.adapters.codex_runner import read_rollout_usage
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.contracts import Money
from issue_agent.domain.pricing import BudgetPolicy
from issue_agent.domain.run_usage import price_run_usage, EstimatedRunUsage

def counter(tokens):
    return event('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': tokens, 'output_tokens': 0}}})

def receipt(sid, response, tokens, cumulative=None):
    payload = {'thread_id': sid, 'turn_id': 't', 'response_id': response, 'usage': {'input_tokens': tokens, 'output_tokens': 0}}
    if cumulative is not None:
        payload['thread_token_usage'] = {'input_tokens': cumulative, 'output_tokens': 0}
    return event('token_usage_record', payload)

def child_rollout(cwd, events):
    return [metadata(CHILD, cwd, ROOT), event('turn_context', {'turn_id': 't', 'model': 'sol'}), *events, event('event_msg', {'type': 'task_complete', 'turn_id': 't'})]

def test_delayed_linked_receipt_keeps_other_cumulative_spend_and_gap(tmp_path):
    home = tmp_path / 'codex'
    write(home, ROOT, [metadata(ROOT, tmp_path), event('turn_context', {'turn_id': 't', 'model': 'sol'}), counter(200), receipt(ROOT, 'second', 100, 200)])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, 'sol', None, final=True)
    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status='pushed', run_usage=observed)))
    wrapper = replace(wrapper, rate_table=RATES)
    wrapper.set_context('fix', 'RESOLVE', 1, 1)
    _run(wrapper, RuntimePlan('work', cwd=str(tmp_path), model='sol', max_budget_usd=2))
    with _db(store) as db:
        charge = tuple(db.execute("SELECT charged_micros,basis FROM legs WHERE state='ended'").fetchone())
    assert charge == (400, 'estimate')

@pytest.mark.parametrize('receipt_first', [False, True])
def test_cumulative_only_and_two_legacy_receipts_settle_once(tmp_path, receipt_first):
    home = tmp_path / 'codex'
    receipts = [receipt(CHILD, 'first', 100), receipt(CHILD, 'second', 100)]
    events = receipts + [counter(200)] if receipt_first else [counter(200)] + receipts
    write(home, ROOT, [metadata(ROOT, tmp_path), *spawn()])
    write(home, CHILD, child_rollout(tmp_path, events))
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, 'sol', None, final=True)
    store, scope, run_id = native._reserved_store(tmp_path, runtime='codex')
    home, evidence = native.recovery_files(tmp_path)
    write(home, ROOT, [metadata(ROOT, tmp_path), *spawn()])
    write(home, CHILD, child_rollout(tmp_path, events))
    native.write_spend_evidence(str(tmp_path / 'evidence'), str(run_id), evidence)
    native._end_run_at(store, run_id, native.ENDED)
    reconciler = native.CodexSpendReconciler(store, (str(tmp_path / 'evidence'),))
    assert reconciler.reconcile(run_id)
    reconciler.reconcile(run_id)
    with _db(store) as db:
        assert db.execute("SELECT count(*) FROM legs WHERE state='ended'").fetchone()[0] == 1
    assert store.snapshot(scope).settled == Money('USD', 400)
    assert price_run_usage(observed, RATES).micros == 400

@pytest.mark.parametrize("missing_prior_counter", [False, True])
def test_delayed_child_receipt_does_not_hide_live_cap_overrun(tmp_path, monkeypatch, missing_prior_counter):
    if missing_prior_counter:
        linked = receipt(CHILD, "new", 100, 200)
        linked["payload"]["turn_id"] = "new"
        records = [metadata(CHILD, tmp_path, ROOT),
            event("turn_context", {"turn_id": "old", "model": "sol"}),
            event("event_msg", {"type": "task_complete", "turn_id": "old"}),
            event("turn_context", {"turn_id": "new", "model": "luna"}), linked]
        cap, charge = 1250, 1360
    else:
        records = child_rollout(tmp_path, [counter(200), receipt(CHILD, "second", 100, 200)])
        cap, charge = 1450, 1550
    monkeypatch.setattr(native, 'child_records', lambda *_args, **_kwargs: records)
    directory = tmp_path / 'evidence'
    logs = []
    runner = replace(native.native_runner(tmp_path, native.native_executable(tmp_path), directory), log=logs.append)
    wrapper, store = _wrapper(tmp_path, runner, evidence_dir=str(directory))
    wrapper = replace(wrapper, rate_table=RATES, budget_policy=BudgetPolicy({('fix', 'RESOLVE'): Money('USD', cap)}))
    wrapper.set_context('fix', 'RESOLVE', 1, 1)
    result = _run(wrapper, RuntimePlan('work', cwd=str(tmp_path), model='sol', max_budget_usd=cap / 1_000_000, env={'CODEX_HOME': str(tmp_path / 'codex')}))
    with _db(store) as db:
        assert tuple(db.execute("SELECT charged_micros,basis FROM legs WHERE state='ended'").fetchone()) == (charge, 'estimate')
    assert not (tmp_path / 'finished').exists()
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED

def test_legacy_recovery_prices_surviving_usage_at_frozen_legacy_rates(tmp_path):
    store, scope, run_id = native._reserved_store(tmp_path, runtime='codex')
    home = tmp_path / 'codex'
    write(home, ROOT, [metadata(ROOT, tmp_path), event('turn_context', {'turn_id': 't', 'model': 'sol'}), counter(100)])
    evidence = {'version': 1, 'cwd': str(tmp_path), 'rollout_started_at': START, 'session_id': ROOT, 'codex_home': str(home), 'model': 'sol', 'baseline_session_id': '', 'baseline_usage': None, 'cost_per_mtok_in': 2, 'cost_per_mtok_out': 10, 'cost_per_mtok_cached_in': 0.1}
    directory = tmp_path / 'evidence'
    native.write_spend_evidence(str(directory), str(run_id), evidence)
    native._end_run_at(store, run_id, native.ENDED)
    reconciler = native.CodexSpendReconciler(store, (str(directory),))
    assert reconciler.reconcile(run_id)
    assert store.snapshot(scope).settled == Money('USD', 200)


@pytest.mark.parametrize("order", tuple(permutations(range(3))))
@pytest.mark.parametrize("participant", [ROOT, CHILD])
def test_every_token_category_and_receipt_replay_are_order_independent(tmp_path, order, participant):
    home = tmp_path / "codex"
    cumulative = {"input_tokens": 240, "cached_input_tokens": 60,
                  "cache_write_input_tokens": 20, "output_tokens": 10}
    tokens = {"input_tokens": 200, "cached_input_tokens": 120,
              "cache_write_input_tokens": 40, "output_tokens": 5}
    records = [event("event_msg", {"type": "token_count", "info": {"total_token_usage": cumulative}}),
        event("token_usage_record", {"thread_id": participant, "turn_id": "t", "response_id": "a", "usage": tokens}),
        event("token_usage_record", {"thread_id": participant, "turn_id": "t", "response_id": "b", "usage": tokens})]
    if participant == CHILD:
        write(home, ROOT, [metadata(ROOT, tmp_path), *spawn()])
    write(home, participant, [metadata(participant, tmp_path, ROOT if participant == CHILD else None),
        event("turn_context", {"turn_id": "t", "model": "sol"}),
        *(records[i] for i in order), records[1]])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES) == Money("USD", 644)


@pytest.mark.parametrize("costs,expected", [((.0009, .0011), 2000), ((0, 0), 0), ((.0009, None), 1100), ((.0009, -1), 1100), ((.0009, 10**100), 1100)])
def test_each_reported_cost_wins_over_its_request_tokens(tmp_path, costs, expected):
    home = tmp_path / "codex"
    receipts = [receipt(ROOT, name, 100) for name in ("a", "b")]
    for record, cost in zip(receipts, costs):
        if cost is not None:
            record["payload"]["cost_usd"] = cost
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
        counter(200), *receipts, receipts[0]])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert price_run_usage(observed, RATES) == Money("USD", expected)


def test_cumulative_spend_survives_incomplete_reported_costs(tmp_path):
    home = tmp_path / "codex"
    record = receipt(ROOT, "a", 100, 200)
    record["payload"]["cost_usd"] = .0001
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}), record])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES) == Money("USD", 300)


@pytest.mark.parametrize("prior_counter", [None, 0, 50, 100])
@pytest.mark.parametrize("delayed", [False, True])
def test_cumulative_remainder_keeps_its_observed_model(tmp_path, delayed, prior_counter):
    home = tmp_path / "codex"
    linked = receipt(ROOT, "new", 100, 200)
    linked["payload"]["turn_id"] = "new"
    records = [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "old", "model": "sol"}),
        *([counter(prior_counter)] if prior_counter is not None else []),
        event("turn_context", {"turn_id": "new", "model": "luna"}), linked]
    if delayed:
        late = receipt(ROOT, "old", 100, 100)
        late["payload"]["turn_id"] = "old"
        records.append(late)
    write(home, ROOT, records)
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert price_run_usage(observed, RATES) == Money("USD", 210)


@pytest.mark.parametrize("records", [[receipt(ROOT, "r", 100)], [counter(100), counter(999)]])
def test_legacy_recovery_preserves_receipts_and_excludes_later_session_usage(tmp_path, records):
    store, scope, run_id = native._reserved_store(tmp_path, runtime="codex")
    home = tmp_path / "codex"
    records = [dict(record) for record in records]
    if len(records) == 2:
        records[1]["timestamp"] = (native.ENDED + timedelta(seconds=1)).isoformat()
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}), *records])
    evidence = {"version": 1, "cwd": str(tmp_path), "rollout_started_at": START, "session_id": ROOT,
        "codex_home": str(home), "model": "sol", "baseline_session_id": "", "baseline_usage": None,
        "cost_per_mtok_in": 2, "cost_per_mtok_out": 10, "cost_per_mtok_cached_in": .1}
    native.write_spend_evidence(str(tmp_path / "evidence"), str(run_id), evidence)
    native._end_run_at(store, run_id, native.ENDED)
    assert native.CodexSpendReconciler(store, (str(tmp_path / "evidence"),)).reconcile(run_id)
    assert store.snapshot(scope).settled == Money("USD", 200)


@pytest.mark.parametrize("invalid", ["1000000", True, 1.5, 10**100, -1, None])
def test_raw_parent_snapshot_cannot_launder_malformed_counters(tmp_path, invalid):
    home = tmp_path / "codex"
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
        event("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": invalid, "output_tokens": 0}}})])
    usage = read_rollout_usage(ROOT, str(home))
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", usage, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES) == Money.zero()


def test_legacy_parent_without_model_name_keeps_frozen_scalar_price(tmp_path):
    store, scope, run_id = native._reserved_store(tmp_path, runtime="codex")
    home = tmp_path / "codex"
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}), counter(100)])
    evidence = {"version": 1, "cwd": str(tmp_path), "rollout_started_at": START, "session_id": ROOT,
        "codex_home": str(home), "cost_per_mtok_in": 2, "cost_per_mtok_out": 10, "cost_per_mtok_cached_in": .1}
    native.write_spend_evidence(str(tmp_path / "evidence"), str(run_id), evidence)
    native._end_run_at(store, run_id, native.ENDED)
    assert native.CodexSpendReconciler(store, (str(tmp_path / "evidence"),)).reconcile(run_id)
    assert store.snapshot(scope).settled == Money("USD", 200)


@pytest.mark.parametrize("timestamp", [None, "2026-10-12T00:00:00", "invalid", 123])
def test_live_snapshot_cannot_price_unwindowed_counters(tmp_path, timestamp):
    home = tmp_path / "codex"
    invalid = counter(1_000_000)
    invalid["timestamp"] = timestamp
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}), counter(100), invalid])
    usage = read_rollout_usage(ROOT, str(home))
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", usage, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES) == Money("USD", 200)



@pytest.mark.parametrize("markers", [False, True])
def test_unmodeled_root_turn_retains_its_trusted_launch_model(tmp_path, markers):
    home = tmp_path / "codex"
    linked = receipt(ROOT, "new", 100, 200)
    linked["payload"]["turn_id"] = "new"
    write(home, ROOT, [metadata(ROOT, tmp_path),
        *([event("event_msg", {"type": "task_started", "turn_id": "old"}),
           event("event_msg", {"type": "task_complete", "turn_id": "old"})] if markers else []),
        event("turn_context", {"turn_id": "new", "model": "luna"}), linked])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert price_run_usage(observed, RATES) == Money("USD", 210)
