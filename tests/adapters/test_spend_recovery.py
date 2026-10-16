from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.codex_runner import rate_table_evidence, write_spend_evidence
from issue_agent.domain.pricing import ModelRates, RateTable
from issue_agent.adapters.control_store import (
    AccountingDisposition,
    BudgetLimit,
    ReservationRequest,
    SQLiteControlStore,
)
from issue_agent.adapters.spend_recovery import CodexSpendReconciler
from issue_agent.domain.contracts import BudgetScope, Money, RunId


NOW = datetime.now(timezone.utc)
OWNER = "o/r:fix"


def _reserved_store(tmp_path, *, runtime="codex"):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    scope = BudgetScope(
        "o/r",
        "repository",
        "USD",
        "2026-07-30",
        NOW.replace(hour=0),
        NOW.replace(hour=0) + timedelta(days=1),
        ZoneInfo("UTC"),
    )
    run_id = RunId("crashed-run")
    assert store.lease("o/r", "fix", 7, OWNER)
    request = ReservationRequest(
        run_id,
        "o/r",
        "fix",
        "RESOLVE",
        OWNER,
        Money("USD", 1_000_000),
        (BudgetLimit(scope, Money("USD", 2_000_000)),),
        7,
        issue=7,
        runtime=runtime,
    )
    assert store.reserve(request).admitted
    return store, scope, run_id


@pytest.mark.parametrize(
    "card,expected", [({}, ("codex-config", "cfg")), ({"version": 2}, ("codex-config", "effective-prices"))]
)
def test_reconciles_crashed_codex_reservation_from_rollout_usage(tmp_path, monkeypatch, card, expected):
    store, scope, run_id = _reserved_store(tmp_path)
    settlements = []
    settle = store.settle

    def record_settlement(request):
        settlements.append(request)
        return settle(request)

    monkeypatch.setattr(store, "settle", record_settlement)
    codex_home = tmp_path / "codex-home"
    _write_rollout(
        codex_home, "session-1", [_count({"input_tokens": 1000, "output_tokens": 200, "cached_input_tokens": 400})]
    )
    evidence_dir = tmp_path / "state" / "spend-evidence"
    write_spend_evidence(
        str(evidence_dir),
        str(run_id),
        {
            "cwd": str(tmp_path),
            "rollout_started_at": NOW.timestamp(),
            "session_id": "session-1",
            "baseline_session_id": "",
            "baseline_usage": None,
            "model": "gpt",
            "codex_home": str(codex_home),
            "rate_table": rate_table_evidence(RateTable(expected[0], expected[1], "USD", CARD.rates)),
            **card,
        },
    )

    _end_run_at(store, run_id, END)
    assert CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)

    assert store.active_reservation_runs(OWNER) == ()
    assert store.snapshot(scope).settled == Money("USD", 10_400)
    assert (settlements[0].rate_card_id, settlements[0].rate_card_version) == expected
    assert not any(evidence_dir.iterdir())


@pytest.mark.parametrize("invalid", [
    {"input_tokens": "1000"},
    {"input_tokens": -1},
    {"output_tokens": 10**12 + 1},
    {"cached_input_tokens": "x"},
    {"cached_input_tokens": 1001},
    {"cache_write_input_tokens": 1001},
    {"cached_input_tokens": 600, "cache_write_input_tokens": 401},
])
def test_a_recovered_leg_whose_counters_are_malformed_settles_its_reserved_maximum(tmp_path, invalid):
    """Fail closed: unknown spend bounded by the reservation is charged at
    that bound, never at zero."""
    store, scope, run_id = _reserved_store(tmp_path)
    codex_home = tmp_path / "codex-home"
    _write_rollout(codex_home, "session-1", [
        _count({"input_tokens": 1000, "output_tokens": 200}),
        _count({"input_tokens": 1000, "output_tokens": 200, **invalid}),
    ])
    evidence_dir = tmp_path / "state" / "spend-evidence"
    write_spend_evidence(
        str(evidence_dir),
        str(run_id),
        {
            "cwd": str(tmp_path),
            "rollout_started_at": NOW.timestamp(),
            "session_id": "session-1",
            "baseline_session_id": "",
            "baseline_usage": None,
            "model": "gpt",
            "codex_home": str(codex_home),
            "rate_table": None,
            "max_budget_usd": 1.0,
        },
    )

    _end_run_at(store, run_id, END)
    assert CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)

    assert store.active_reservation_runs(OWNER) == ()
    assert store.snapshot(scope).settled == Money("USD", 1_000_000)


def test_missing_rollout_does_not_fabricate_recovered_spend(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path)
    evidence_dir = tmp_path / "state" / "spend-evidence"
    write_spend_evidence(
        str(evidence_dir),
        str(run_id),
        {
            "version": 1,
            "cwd": str(tmp_path),
            "rollout_started_at": NOW.timestamp(),
            "session_id": "missing",
            "cost_per_mtok_in": 10,
            "cost_per_mtok_out": 20,
            "cost_per_mtok_cached_in": 1,
        },
    )

    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs(OWNER) == (run_id,)


def test_legacy_receipt_without_native_invocation_contract_settles_estimate(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path)
    evidence_dir = tmp_path / "spend-evidence"
    write_spend_evidence(
        str(evidence_dir),
        str(run_id),
        {
            "version": 1,
            "cwd": str(tmp_path),
            "rollout_started_at": NOW.timestamp(),
            "session_id": "prior-session",
            "cost_per_mtok_in": 10,
            "cost_per_mtok_out": 20,
            "cost_per_mtok_cached_in": 1,
        },
    )
    store.record_reservation_retained(run_id, "prior completed unmeasured run")
    assert CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs(OWNER) == ()


def test_unavailable_recovery_preserves_proven_unlaunched_release(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    assert not CodexSpendReconciler(store, (str(tmp_path / "missing-evidence"),)).reconcile(run_id)
    result = store.release_unlaunched(run_id, "provider was never launched")
    assert result.disposition is AccountingDisposition.AVAILABLE
    assert store.active_reservation_runs(OWNER) == ()


@pytest.mark.parametrize("runtime", ["claude"])
def test_codex_recovery_does_not_change_other_providers_unmeasured_policy(tmp_path, runtime):
    store, _scope, run_id = _reserved_store(tmp_path, runtime=runtime)
    store.record_reservation_retained(run_id, "provider return is unmeasured")
    assert not CodexSpendReconciler(store, (str(tmp_path / "missing-codex-evidence"),)).reconcile(run_id)
    assert store.active_reservation_runs(OWNER) == (run_id,)
    assert store.settle_terminal_estimate(run_id, "existing completed unmeasured grace elapsed")
    assert store.active_reservation_runs(OWNER) == ()


def test_running_codex_uncertainty_survives_generic_completion_and_grace(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    store.register_process_lease(run_id, expected_owner_id=OWNER, pid=4242, pgid=4242, process_start_ticks=7)
    assert not CodexSpendReconciler(store, (str(tmp_path / "missing-evidence"),)).reconcile(run_id)
    store.record_reservation_retained(run_id, "generic post-run retention")
    assert store.settle_terminal_estimate(run_id, "process returned without evidence")
    assert store.active_reservation_runs(OWNER) == ()


ROOT = "019fa244-0000-7de0-8e18-e56f423bbbcc"
CARD = RateTable("card", "v1", "USD", {"gpt": ModelRates(10000000, 20000000, 1000000, 10000000)})
# 500 uncached in x $10 + 10 out x $20 + 500 cached x $1 per MTok, rounded up per class.
ROOT_MICROS = 5_700


def _ev(kind, payload, at=None):
    return {"timestamp": (at or NOW).isoformat(), "type": kind, "payload": payload}


def _count(total, at=None):
    return _ev("event_msg", {"type": "token_count", "info": {"total_token_usage": total}}, at)


def _write_rollout(home, sid, records):
    directory = home / "sessions" / NOW.strftime("%Y/%m/%d")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"rollout-now-{sid}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in records))


def _home(tmp_path, *records):
    home = tmp_path / "codex-home"
    _write_rollout(
        home, ROOT, [_count({"input_tokens": 1000, "output_tokens": 10, "cached_input_tokens": 500}), *records]
    )
    return home


def _evidence(tmp_path, home, **overrides):
    evidence_dir = tmp_path / "state" / "spend-evidence"
    write_spend_evidence(
        str(evidence_dir),
        "crashed-run",
        {
            "cwd": str(tmp_path),
            "rollout_started_at": NOW.timestamp() - 5,
            "session_id": ROOT,
            "baseline_session_id": "",
            "baseline_usage": None,
            "model": "gpt",
            "codex_home": str(home),
            "rate_table": rate_table_evidence(CARD),
            **overrides,
        },
    )
    return evidence_dir


def _end_run_at(store, run_id, at):
    """The leg's process ended at ``at`` and gave up its lease, unsettled."""
    with closing(sqlite3.connect(store.path)) as db:
        db.execute(
            "UPDATE legs SET ended_at=? WHERE leg_id=?",
            (at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), str(run_id)),
        )
        db.execute("DELETE FROM leases WHERE leg_id=?", (str(run_id),))
        db.commit()


END = NOW.replace(microsecond=0) + timedelta(seconds=10)


def _recover(tmp_path, home, *, measured=True, **overrides):
    store, scope, run_id = _reserved_store(tmp_path, runtime="codex")
    _end_run_at(store, run_id, END)
    evidence_dir = _evidence(tmp_path, home, **overrides)
    assert CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs(OWNER) == ()
    # Unusable evidence stays on disk for the operator; consumed evidence goes.
    assert any(evidence_dir.iterdir()) is not measured
    return store.snapshot(scope).settled


def test_recovery_prices_the_root_counter_at_the_frozen_card(tmp_path):
    assert _recover(tmp_path, _home(tmp_path)) == Money("USD", ROOT_MICROS)


def test_recovery_with_2000_unrelated_rollouts_settles_owned_usage(tmp_path):
    home = _home(tmp_path)
    for i in range(2000):
        _write_rollout(home, f"019fa244-0000-7de0-8e18-{i:012x}", [_count({"input_tokens": 9, "output_tokens": 9})])
    assert _recover(tmp_path, home) == Money("USD", ROOT_MICROS)


def test_a_counter_exactly_at_the_end_counts_and_one_millisecond_later_does_not(tmp_path):
    total = {"input_tokens": 2000, "output_tokens": 10, "cached_input_tokens": 500}
    at_end = _recover(tmp_path / "a", _home(tmp_path / "a", _count(total, END)))
    after = _recover(tmp_path / "b", _home(tmp_path / "b", _count(total, END + timedelta(milliseconds=1))))
    assert (at_end, after) == (Money("USD", ROOT_MICROS + 10_000), Money("USD", ROOT_MICROS))


def test_an_untimestamped_counter_is_not_this_runs_evidence(tmp_path):
    home = _home(tmp_path)
    with next(home.rglob(f"*{ROOT}.jsonl")).open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 10**9, "output_tokens": 10**9}},
                    },
                }
            )
            + "\n"
        )
    assert _recover(tmp_path, home) == Money("USD", ROOT_MICROS)


def test_the_baseline_is_subtracted_only_from_its_own_session(tmp_path):
    baseline = {
        "input_tokens": 400,
        "output_tokens": 5,
        "cache_read_input_tokens": 200,
        "cache_creation_input_tokens": 0,
        "source": "rollout",
    }
    resumed = _recover(tmp_path / "a", _home(tmp_path / "a"), baseline_session_id=ROOT, baseline_usage=baseline)
    other = _recover(tmp_path / "b", _home(tmp_path / "b"), baseline_session_id="another", baseline_usage=baseline)
    # The resumed delta: 300 uncached in, 5 out, 300 cached.
    assert (resumed, other) == (Money("USD", 3_000 + 100 + 300), Money("USD", ROOT_MICROS))


@pytest.mark.parametrize("damage", ["missing_home", "malformed_rollout", "no_session"])
def test_unusable_rollouts_settle_a_marked_zero_estimate(tmp_path, damage):
    home = _home(tmp_path)
    overrides = {}
    if damage == "missing_home":
        home = tmp_path / "nowhere"
    elif damage == "malformed_rollout":
        next(home.rglob(f"*{ROOT}.jsonl")).write_text("{not json\n")
    else:
        overrides = {"session_id": ""}
    assert _recover(tmp_path, home, **overrides) == Money("USD", 0)


def test_malformed_evidence_settles_a_zero_estimate(tmp_path):
    assert _recover(tmp_path, _home(tmp_path), measured=False, rate_table={"id": "x"}) == Money("USD", 0)


def test_recovery_price_exceeding_sqlite_bound_settles_zero_estimate(tmp_path):
    huge = {"input_tokens": 10**12, "output_tokens": 10**12, "cached_input_tokens": 0}
    card = RateTable("card", "v1", "USD", {"gpt": ModelRates(*(10**15,) * 4)})
    home = _home(tmp_path, _count(huge))
    assert _recover(tmp_path, home, measured=False, rate_table=rate_table_evidence(card)) == Money("USD", 0)


def test_recovery_never_settles_without_the_runs_recorded_end(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path, runtime="codex")
    evidence_dir = _evidence(tmp_path, _home(tmp_path))

    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)

    assert store.active_reservation_runs(OWNER) == (run_id,)
    assert store.snapshot(scope).settled == Money("USD", 0)
