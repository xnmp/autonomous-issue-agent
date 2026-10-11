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
from issue_agent.adapters.event_log import EventLog
from issue_agent.adapters.spend_recovery import CodexSpendReconciler
from issue_agent.domain.events import NativeUsageRemetered, Producer, decode_event_line
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


ROOT = "019fa244-0000-7de0-8e18-e56f423bbbcc"
CHILD = "019fa244-0000-7de0-8e18-e56f423bbbcd"
AMBIGUOUS = "native response usage is missing or token counts are ambiguous"
CHILD_TOKENS = {"input_tokens": 1000, "cached_input_tokens": 400, "cache_write_input_tokens": 0, "output_tokens": 20}


def _ev(kind, payload):
    return {"timestamp": NOW.isoformat(), "type": kind, "payload": payload}


def _count(last, total):
    return _ev("event_msg", {"type": "token_count", "info": {"last_token_usage": last, "total_token_usage": total},
                              "rate_limits": {"used": 1}})


def _write_rollout(home, sid, records):
    directory = home / "sessions" / NOW.strftime("%Y/%m/%d")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"rollout-now-{sid}.jsonl").write_text(
        "".join(json.dumps({**e, "ordinal": i}) + "\n" for i, e in enumerate(records)))


def _meta(sid, cwd, parent=None):
    source = ({"subagent": {"thread_spawn": {"parent_thread_id": parent, "agent_path": "/root/review"}}}
              if parent else "exec")
    return _ev("session_meta", {"id": sid, "session_id": parent or sid, "source": source, "originator": "codex_exec",
                                "cwd": str(cwd), "cli_version": "0.160.0", "subagent_history_start_ordinal": None})


def _native_home(tmp_path, extra_counts=()):
    home = tmp_path / "codex-home"
    root_total = {"input_tokens": 1000, "output_tokens": 10, "cached_input_tokens": 500}
    _write_rollout(home, ROOT, [
        _meta(ROOT, tmp_path),
        _ev("response_item", {"type": "function_call", "name": "spawn_agent", "call_id": "c1",
                              "arguments": json.dumps({"task_name": "review", "message": "go"})}),
        _ev("response_item", {"type": "function_call_output", "call_id": "c1",
                              "output": json.dumps({"task_name": "/root/review", "nickname": None})}),
        _count(root_total, root_total)])
    turn = "turn:child"
    child = [_meta(CHILD, tmp_path, ROOT),
             _ev("event_msg", {"type": "task_started", "turn_id": turn}),
             _ev("turn_context", {"turn_id": turn, "model": "gpt"}),
             _ev("token_usage_record", {"thread_id": CHILD, "turn_id": turn, "response_id": "r1",
                                        "usage": CHILD_TOKENS, "thread_token_usage": CHILD_TOKENS}),
             _count(CHILD_TOKENS, CHILD_TOKENS), *extra_counts,
             _ev("event_msg", {"type": "task_complete", "turn_id": turn})]
    _write_rollout(home, CHILD, child)
    return home


def _frozen_evidence(tmp_path, home, failure):
    evidence_dir = tmp_path / "state" / "spend-evidence"
    write_spend_evidence(str(evidence_dir), "crashed-run", {
        "cwd": str(tmp_path), "rollout_started_at": NOW.timestamp() - 5, "session_id": ROOT,
        "baseline_session_id": "", "baseline_usage": None, "model": "gpt",
        "cost_per_mtok_in": 10, "cost_per_mtok_out": 20, "cost_per_mtok_cached_in": 1,
        "codex_home": str(home), "config_version": "cfg", "native_usage_started_at": NOW.timestamp() - 5,
        "native_root_sessions": {ROOT: "gpt"},
        "native_rate_table": rate_table_evidence(RateTable(
            "card", "v1", "USD", {"gpt": ModelRates(10000000, 20000000, 1000000, 10000000)})),
        "native_usage_failure": failure})
    return evidence_dir


def test_frozen_ambiguous_failure_is_remetered_from_reemission_rollout_and_settles(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path, runtime="codex")
    log = EventLog(str(tmp_path / "events-v2.jsonl"), Producer("deployment", "o/r", "fix", 100, "boot"))
    store.bind_event_outbox(log.prepare, log.append_committed)
    assert store.record_accounting_unavailable(run_id, AMBIGUOUS)
    home = _native_home(tmp_path, extra_counts=[_count(CHILD_TOKENS, CHILD_TOKENS)])
    evidence_dir = _frozen_evidence(tmp_path, home, AMBIGUOUS)

    assert CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)

    assert store.active_reservation_runs() == ()
    assert store.snapshot(scope).settled.micros > 0
    assert not any(evidence_dir.iterdir())
    payloads = [decode_event_line(line).payload for line in (tmp_path / "events-v2.jsonl").read_text().splitlines()]
    assert NativeUsageRemetered(str(run_id), "AMBIGUOUS_TOKEN_COUNTS", "measured") in payloads


def test_frozen_ambiguous_failure_with_genuinely_conflicting_rollout_stays_unavailable(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    doubled = {k: v * 2 for k, v in CHILD_TOKENS.items()}
    home = _native_home(tmp_path, extra_counts=[_count(CHILD_TOKENS, doubled)])
    evidence_dir = _frozen_evidence(tmp_path, home, AMBIGUOUS)

    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs() == (run_id,) and any(evidence_dir.iterdir())


@pytest.mark.parametrize("failure", ["native child has no corroborating rollout", "something unclassified"])
def test_other_frozen_failures_are_final_without_reading_rollouts(tmp_path, failure):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    home = _native_home(tmp_path)  # would meter cleanly if it were read
    evidence_dir = _frozen_evidence(tmp_path, home, failure)

    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs() == (run_id,)


@pytest.mark.parametrize("damage", ["missing_home", "missing_child_rollout", "malformed_rollout"])
def test_frozen_ambiguous_failure_fails_closed_when_rollouts_are_unusable(tmp_path, damage):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    home = _native_home(tmp_path)
    if damage == "missing_home":
        home = tmp_path / "nowhere"
    elif damage == "missing_child_rollout":
        next(home.rglob(f"*{CHILD}.jsonl")).unlink()
    else:
        next(home.rglob(f"*{CHILD}.jsonl")).write_text("{not json\n")
    evidence_dir = _frozen_evidence(tmp_path, home, AMBIGUOUS)

    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs() == (run_id,)
