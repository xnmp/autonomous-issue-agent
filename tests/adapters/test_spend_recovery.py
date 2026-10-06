from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.codex_runner import write_spend_evidence
from issue_agent.adapters.codex_child_usage import rate_table_evidence
from issue_agent.domain.pricing import ModelRates, RateTable
from issue_agent.adapters.control_store import (
    ReservationRequest,
    SQLiteControlStore,
    AccountingDisposition,
)
from issue_agent.adapters.spend_recovery import CodexSpendReconciler
from issue_agent.domain.contracts import (
    BudgetScope,
    Money,
    PriceQuote,
    ReservationId,
    RunId,
)


NOW = datetime.now(timezone.utc)


def _reserved_store(tmp_path, *, runtime=None):
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
    request = ReservationRequest(
        ReservationId("reservation"),
        run_id,
        json.dumps({"runtime": runtime}) if runtime else "dispatch",
        scope,
        PriceQuote("gpt", "card", "v1", NOW, Money("USD", 1_000_000)),
        Money("USD", 2_000_000),
        "cfg",
        "o/r",
    )
    assert store.reserve(request).admitted
    return store, scope, run_id


@pytest.mark.parametrize("card,expected", [
    ({}, ("codex-config", "cfg")),
    ({"rate_card_id": "codex-config", "rate_card_version": "effective-prices"},
     ("codex-config", "effective-prices")),
])
def test_reconciles_crashed_codex_reservation_from_rollout_usage(tmp_path, monkeypatch, card, expected):
    store, scope, run_id = _reserved_store(tmp_path)
    settlements = []
    settle = store.settle

    def record_settlement(request):
        settlements.append(request)
        return settle(request)

    monkeypatch.setattr(store, "settle", record_settlement)
    codex_home = tmp_path / "codex-home"
    rollout_dir = codex_home / "sessions" / NOW.strftime("%Y/%m/%d")
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-now-session-1.jsonl").write_text(
        '{"type":"event_msg","payload":{"type":"token_count","info":'
        '{"total_token_usage":{"input_tokens":1000,"output_tokens":200,'
        '"cached_input_tokens":400}}}}\n'
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
            "cost_per_mtok_in": 10,
            "cost_per_mtok_out": 20,
            "cost_per_mtok_cached_in": 1,
            "codex_home": str(codex_home),
            "config_version": "cfg",
            "native_usage_started_at": NOW.timestamp(),
            "native_root_sessions": {},
            "native_rate_table": rate_table_evidence(RateTable(
                expected[0], expected[1], "USD", {"gpt": ModelRates(10000000, 20000000, 1000000, 10000000)})),
            "native_usage_failure": "",
            **card,
        },
    )

    assert CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)

    assert store.active_reservation_runs() == ()
    assert store.snapshot(scope).settled == Money("USD", 10_400)
    assert (settlements[0].rate_card_id, settlements[0].rate_card_version) == expected
    assert not any(evidence_dir.iterdir())


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

    assert not CodexSpendReconciler(
        store, (str(evidence_dir),)
    ).reconcile(run_id)
    assert store.active_reservation_runs() == (run_id,)


def test_legacy_receipt_retains_obligation_without_native_invocation_contract(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path)
    evidence_dir = tmp_path / "spend-evidence"
    write_spend_evidence(str(evidence_dir), str(run_id), {
        "version": 1, "cwd": str(tmp_path), "rollout_started_at": NOW.timestamp(),
        "session_id": "prior-session", "cost_per_mtok_in": 10,
        "cost_per_mtok_out": 20, "cost_per_mtok_cached_in": 1,
    })
    store.record_reservation_retained(run_id, "prior completed unmeasured run")
    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.release_retained(run_id, "unmeasured grace elapsed", NOW + timedelta(days=365)).disposition is AccountingDisposition.CONFLICT
    assert store.active_reservation_runs() == (run_id,)


def test_unavailable_recovery_preserves_proven_unlaunched_release(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    assert not CodexSpendReconciler(store, (str(tmp_path / "missing-evidence"),)).reconcile(run_id)
    result = store.release_unlaunched(run_id, "provider was never launched")
    assert result.disposition is AccountingDisposition.AVAILABLE
    assert store.active_reservation_runs() == ()


@pytest.mark.parametrize("runtime", ["claude", "glm"])
def test_codex_recovery_does_not_change_other_providers_unmeasured_policy(tmp_path, runtime):
    store, _scope, run_id = _reserved_store(tmp_path, runtime=runtime)
    store.record_reservation_retained(run_id, "provider return is unmeasured")
    assert not CodexSpendReconciler(store, (str(tmp_path / "missing-codex-evidence"),)).reconcile(run_id)
    assert store.release_retained(run_id, "existing completed unmeasured grace elapsed",
                                  NOW + timedelta(days=365)).disposition is AccountingDisposition.AVAILABLE
    assert store.active_reservation_runs() == ()


def test_running_codex_uncertainty_survives_generic_completion_and_grace(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    with store.transaction() as db:
        db.execute("UPDATE runs SET state='running' WHERE run_id=?", (str(run_id),))
    assert not CodexSpendReconciler(store, (str(tmp_path / "missing-evidence"),)).reconcile(run_id)
    store.record_reservation_retained(run_id, "generic post-run retention")
    assert store.release_retained(run_id, "completed unmeasured grace elapsed",
                                  NOW + timedelta(days=365)).disposition is AccountingDisposition.CONFLICT
    assert store.active_reservation_runs() == (run_id,)
