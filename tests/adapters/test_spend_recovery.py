from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from issue_agent.adapters.codex_runner import write_spend_evidence
from issue_agent.adapters.control_store import (
    ReservationRequest,
    SQLiteControlStore,
)
from issue_agent.adapters.spend_recovery import CodexSpendReconciler
from issue_agent.domain.contracts import (
    BudgetScope,
    Money,
    PriceQuote,
    ReservationId,
    RunId,
)


NOW = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)


def _reserved_store(tmp_path):
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
        "dispatch",
        scope,
        PriceQuote("gpt", "card", "v1", NOW, Money("USD", 1_000_000)),
        Money("USD", 2_000_000),
        "cfg",
    )
    assert store.reserve(request).admitted
    return store, scope, run_id


def test_reconciles_crashed_codex_reservation_from_rollout_usage(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path)
    codex_home = tmp_path / "codex-home"
    rollout_dir = codex_home / "sessions" / "2026" / "07" / "30"
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
        },
    )

    assert CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)

    assert store.active_reservation_runs() == ()
    assert store.snapshot(scope).settled == Money("USD", 10_400)
    assert not any(evidence_dir.iterdir())


def test_missing_rollout_does_not_fabricate_recovered_spend(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path)
    evidence_dir = tmp_path / "state" / "spend-evidence"
    write_spend_evidence(
        str(evidence_dir),
        str(run_id),
        {
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
