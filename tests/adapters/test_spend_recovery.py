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
from issue_agent.adapters.codex_child_usage import CodexChildUsageMeter
from issue_agent.domain.run_usage import MeasuredRunUsage, UnavailableRunUsage, price_run_usage
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


def _end_run_at(store, run_id, at):
    with store.transaction() as db:
        db.execute("UPDATE runs SET state='completed', completed_at=? WHERE run_id=?",
                   (at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), str(run_id)))


def _later(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat()


END = NOW.replace(microsecond=0) + timedelta(seconds=10)


def test_frozen_ambiguous_failure_is_remetered_from_reemission_rollout_and_settles(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path, runtime="codex")
    _end_run_at(store, run_id, END)
    log = EventLog(str(tmp_path / "events-v2.jsonl"), Producer("deployment", "o/r", "fix", 100, "boot"))
    store.bind_event_outbox(log.prepare, log.append_committed)
    assert store.record_accounting_unavailable(run_id, AMBIGUOUS)
    home = _native_home(tmp_path, extra_counts=[_count(CHILD_TOKENS, CHILD_TOKENS)])
    evidence_dir = _frozen_evidence(tmp_path, home, AMBIGUOUS)

    assert CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)

    assert store.active_reservation_runs() == ()
    # root 500 uncached in/10 out/500 cached + child 600/20/400 counted once.
    assert store.snapshot(scope).settled == Money("USD", 12_500)
    assert not any(evidence_dir.iterdir())
    payloads = [decode_event_line(line).payload for line in (tmp_path / "events-v2.jsonl").read_text().splitlines()]
    assert NativeUsageRemetered(str(run_id), "AMBIGUOUS_TOKEN_COUNTS", "measured") in payloads


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
    _end_run_at(store, run_id, END)
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


def _append(path, records):
    with path.open("a") as stream:
        stream.write("".join(json.dumps(e) + "\n" for e in records))


def test_later_leg_children_after_the_end_are_not_billed_to_the_ended_run(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path, runtime="codex")
    _end_run_at(store, run_id, END)
    home = _native_home(tmp_path, extra_counts=[_count(CHILD_TOKENS, CHILD_TOKENS)])
    other = "019fa244-0000-7de0-8e18-e56f423bbbcf"
    at = {"timestamp": _later(60)}
    _append(next(home.rglob(f"*{ROOT}.jsonl")), [
        {**at, "type": "response_item", "payload": {
            "type": "function_call", "name": "spawn_agent", "call_id": "c2",
            "arguments": json.dumps({"task_name": "again", "message": "go"})}},
        {**at, "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c2",
            "output": json.dumps({"task_name": "/root/again", "nickname": None})}}])
    turn = "turn:later"
    _write_rollout(home, other, [
        {**_meta(other, tmp_path, ROOT), **at},
        {**_ev("event_msg", {"type": "task_started", "turn_id": turn}), **at},
        {**_ev("turn_context", {"turn_id": turn, "model": "gpt"}), **at},
        {**_ev("token_usage_record", {"thread_id": other, "turn_id": turn, "response_id": "x",
                                      "usage": CHILD_TOKENS, "thread_token_usage": CHILD_TOKENS}), **at}])
    evidence_dir = _frozen_evidence(tmp_path, home, AMBIGUOUS)

    assert CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)

    assert store.snapshot(scope).settled == Money("USD", 12_500)


def _root_count_at(tmp_path, store, run_id, at):
    _end_run_at(store, run_id, END)
    home = _native_home(tmp_path, extra_counts=[_count(CHILD_TOKENS, CHILD_TOKENS)])
    total = {"input_tokens": 2000, "output_tokens": 10, "cached_input_tokens": 500}
    _append(next(home.rglob(f"*{ROOT}.jsonl")), [{**_count(total, total), "timestamp": at.isoformat()}])
    return _frozen_evidence(tmp_path, home, AMBIGUOUS)


def test_root_token_count_exactly_at_the_end_is_counted(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path, runtime="codex")
    evidence_dir = _root_count_at(tmp_path, store, run_id, END)

    assert CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)

    assert store.snapshot(scope).settled == Money("USD", 22_500)


def test_root_token_count_one_millisecond_after_the_end_fails_closed(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    evidence_dir = _root_count_at(tmp_path, store, run_id, END + timedelta(milliseconds=1))

    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs() == (run_id,)


def test_ambiguous_remeter_requires_the_runs_recorded_end(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    assert store.run_completed_at(run_id) is None
    home = _native_home(tmp_path, extra_counts=[_count(CHILD_TOKENS, CHILD_TOKENS)])
    evidence_dir = _frozen_evidence(tmp_path, home, AMBIGUOUS)

    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs() == (run_id,)


def test_remeter_journal_is_emitted_once_across_sweeps(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    log = EventLog(str(tmp_path / "events-v2.jsonl"), Producer("deployment", "o/r", "fix", 100, "boot"))
    store.bind_event_outbox(log.prepare, log.append_committed)
    _end_run_at(store, run_id, END)
    home = _native_home(tmp_path, extra_counts=[_count(CHILD_TOKENS, CHILD_TOKENS)])
    evidence_dir = _frozen_evidence(tmp_path, home, AMBIGUOUS)
    reconciler = CodexSpendReconciler(store, (str(evidence_dir),))

    assert reconciler.reconcile(run_id)
    reconciler.reconcile(run_id)

    payloads = [decode_event_line(line).payload for line in (tmp_path / "events-v2.jsonl").read_text().splitlines()]
    assert sum(isinstance(p, NativeUsageRemetered) for p in payloads) == 1


def test_untimestamped_root_usage_inside_the_bounded_window_fails_closed(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    _end_run_at(store, run_id, NOW + timedelta(seconds=10))
    home = _native_home(tmp_path)
    rollout = next(home.rglob(f"*{ROOT}.jsonl"))
    with rollout.open("a") as stream:
        stream.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 1, "output_tokens": 1}}}}) + "\n")
    evidence_dir = _frozen_evidence(tmp_path, home, AMBIGUOUS)

    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs() == (run_id,)


# --- Operator policy 2026-10-11: price the sum of a session's receipts in the run window ---

UNFINISHED = "native child has unfinished turns or turns without usage"
CARD = RateTable("card", "v1", "USD", {"gpt": ModelRates(10000000, 20000000, 1000000, 10000000)})
ROOT_ONLY_MICROS = 5_700
CHILD_RESPONSE_MICROS = 6_800


def _scaled(n):
    return {k: v * n for k, v in CHILD_TOKENS.items()}


def _killed_child_home(tmp_path, *, responses=2, completed=False, receipts=True, tail=None, late_response=False,
                       in_flight=True):
    """Child killed in a started turn after `responses` receipted requests.

    `in_flight`: a tool result came back after the last receipt, so the next
    model request was outstanding; otherwise the turn was merely awaiting a tool.
    """
    home = _native_home(tmp_path)
    turn = "turn:killed"
    body = [_meta(CHILD, tmp_path, ROOT),
            _ev("event_msg", {"type": "task_started", "turn_id": turn}),
            _ev("turn_context", {"turn_id": turn, "model": "gpt"})]
    for i in range(1, responses + 1):
        if receipts:
            body.append(_ev("token_usage_record", {"thread_id": CHILD, "turn_id": turn, "response_id": f"r{i}",
                                                   "usage": CHILD_TOKENS, "thread_token_usage": CHILD_TOKENS}))
        body.append(_count(CHILD_TOKENS, _scaled(i)))
    if in_flight and responses:
        body.append(_ev("response_item", {"type": "custom_tool_call_output", "call_id": "c", "output": "ok"}))
    if completed:
        body.append(_ev("event_msg", {"type": "task_complete", "turn_id": turn}))
    _write_rollout(home, CHILD, body)
    path = next(home.rglob(f"*{CHILD}.jsonl"))
    if late_response:
        at = {"timestamp": _later(60)}
        _append(path, [{**_ev("token_usage_record", {"thread_id": CHILD, "turn_id": turn, "response_id": "late",
                                                     "usage": CHILD_TOKENS, "thread_token_usage": CHILD_TOKENS}), **at},
                       {**_count(CHILD_TOKENS, _scaled(responses + 1)), **at}])
    if tail is not None:
        with path.open("a") as stream:
            stream.write(tail)
    return home


def _meter(tmp_path, home, **kw):
    return CodexChildUsageMeter(str(home), NOW.timestamp() - 5, CARD, str(tmp_path), **kw)


ROOT_USAGE = {"input_tokens": 1000, "output_tokens": 10, "cache_read_input_tokens": 500,
              "cache_creation_input_tokens": 0, "source": "rollout"}


def _final(meter, **kw):
    return meter.sample(ROOT, "gpt", ROOT_USAGE, **kw)




def _root_home(tmp_path, records):
    home = tmp_path / "codex-home"
    _write_rollout(home, ROOT, [_meta(ROOT, tmp_path), _ev("event_msg", {"type": "task_started", "turn_id": "t"}),
                                _ev("turn_context", {"turn_id": "t", "model": "gpt"}), *records])
    return home


def _root_receipt(n):
    return _ev("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": f"r{n}",
                                      "usage": CHILD_TOKENS, "thread_token_usage": CHILD_TOKENS})


def _counter(n):
    return {"input_tokens": 1000 * n, "output_tokens": 20 * n, "cache_read_input_tokens": 400 * n,
            "cache_creation_input_tokens": 0, "source": "rollout"}


def _root_sample(tmp_path, home, counter):
    return _meter(tmp_path, home).sample(ROOT, "gpt", counter, final=True)




def test_child_killed_mid_turn_is_priced_at_exactly_its_receipts(tmp_path):
    home = _killed_child_home(tmp_path, responses=2)

    observed = _final(_meter(tmp_path, home), final=True)

    assert price_run_usage(observed, CARD).micros == ROOT_ONLY_MICROS + 2 * CHILD_RESPONSE_MICROS


def test_child_unfinished_turn_without_any_receipt_costs_nothing(tmp_path):
    home = _killed_child_home(tmp_path, responses=0)

    assert price_run_usage(_final(_meter(tmp_path, home), final=True), CARD).micros == ROOT_ONLY_MICROS


def test_completed_child_turn_without_receipts_still_fails_closed(tmp_path):
    home = _killed_child_home(tmp_path, responses=1, completed=True, receipts=False)

    assert isinstance(_final(_meter(tmp_path, home), final=True), UnavailableRunUsage)


def test_token_counts_do_not_price_a_receipted_session(tmp_path):
    # The Codex 0/0 token_count quirk and a total that disagrees are irrelevant: receipts are the price.
    zero = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0}
    home = _killed_child_home(tmp_path, responses=2, completed=True, in_flight=False)
    with next(home.rglob(f"*{CHILD}.jsonl")).open("a") as stream:
        stream.write(json.dumps(_count(zero, _scaled(9))) + "\n")

    assert price_run_usage(_final(_meter(tmp_path, home), final=True), CARD).micros == ROOT_ONLY_MICROS + 2 * CHILD_RESPONSE_MICROS


def test_root_with_receipts_is_priced_at_its_receipts_not_its_counter(tmp_path):
    home = _root_home(tmp_path, [_root_receipt(1), _count(CHILD_TOKENS, _scaled(1)), _root_receipt(2)])

    # Counter lags (kill between receipt and count) or is absent: receipts win; same sample repeated is stable.
    meter = _meter(tmp_path, home)
    prices = [price_run_usage(meter.sample(ROOT, "gpt", usage, final=True), CARD).micros
              for usage in (_counter(1), _counter(1), None)]
    assert prices == [2 * CHILD_RESPONSE_MICROS] * 3


def test_root_without_receipts_is_priced_from_its_counter_as_before(tmp_path):
    home = _root_home(tmp_path, [_count(CHILD_TOKENS, _scaled(1))])

    assert price_run_usage(_root_sample(tmp_path, home, _counter(1)), CARD).micros == CHILD_RESPONSE_MICROS
    assert not isinstance(_root_sample(tmp_path, home, None), MeasuredRunUsage)


def test_each_leg_prices_only_the_receipts_inside_its_own_window(tmp_path):
    # Three responses across two legs of one session cost exactly three receipts in total.
    home = _root_home(tmp_path, [_root_receipt(1), _count(CHILD_TOKENS, _scaled(1)), _root_receipt(2)])
    at = {"timestamp": _later(20)}
    _append(next(home.rglob(f"*{ROOT}.jsonl")), [
        {**_ev("event_msg", {"type": "task_started", "turn_id": "u"}), **at},
        {**_ev("turn_context", {"turn_id": "u", "model": "gpt"}), **at},
        {**_ev("token_usage_record", {"thread_id": ROOT, "turn_id": "u", "response_id": "r3",
                                      "usage": CHILD_TOKENS}), **at},
        {**_count(CHILD_TOKENS, _scaled(3)), **at}])
    first = CodexChildUsageMeter(str(home), NOW.timestamp() - 5, CARD, str(tmp_path), ended_at=NOW.timestamp() + 10)
    second = CodexChildUsageMeter(str(home), NOW.timestamp() + 15, CARD, str(tmp_path))

    a = price_run_usage(first.sample(ROOT, "gpt", _counter(1), final=True), CARD).micros
    b = price_run_usage(second.sample(ROOT, "gpt", _counter(2), final=True), CARD).micros

    assert (a, b) == (2 * CHILD_RESPONSE_MICROS, CHILD_RESPONSE_MICROS)


def _boundary_home(tmp_path, stamp):
    home = _root_home(tmp_path, [_root_receipt(1)])
    _append(next(home.rglob(f"*{ROOT}.jsonl")), [
        {**_ev("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": "edge", "usage": CHILD_TOKENS}),
         "timestamp": stamp.isoformat()}])
    return home


def test_receipt_exactly_at_the_end_counts_and_one_millisecond_later_does_not(tmp_path):
    at_end = _boundary_home(tmp_path / "a", END)
    after = _boundary_home(tmp_path / "b", END + timedelta(milliseconds=1))

    for home, count in ((at_end, 2), (after, 1)):
        meter = CodexChildUsageMeter(str(home), NOW.timestamp() - 5, CARD, str(home.parent), ended_at=END.timestamp())
        assert price_run_usage(meter.sample(ROOT, "gpt", None, final=True), CARD).micros == count * CHILD_RESPONSE_MICROS


def test_receipt_before_the_window_starts_is_not_charged(tmp_path):
    home = _root_home(tmp_path, [_root_receipt(1)])
    meter = CodexChildUsageMeter(str(home), NOW.timestamp() + 60, CARD, str(tmp_path))

    assert not isinstance(meter.sample(ROOT, "gpt", None, final=True), MeasuredRunUsage)


@pytest.mark.parametrize("fault", ["negative", "missing_usage", "foreign_thread", "unpriced_child_model"])
def test_malformed_receipt_fails_closed(tmp_path, fault):
    home = _killed_child_home(tmp_path, responses=1)
    path = next(home.rglob(f"*{CHILD}.jsonl"))
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    receipt = next(e for e in lines if e["type"] == "token_usage_record")
    if fault == "negative":
        receipt["payload"]["usage"]["input_tokens"] = -1
    elif fault == "missing_usage":
        del receipt["payload"]["usage"]
    elif fault == "foreign_thread":
        receipt["payload"]["thread_id"] = ROOT
    else:
        next(e for e in lines if e["type"] == "turn_context")["payload"]["model"] = "unpriced"
    path.write_text("".join(json.dumps(e) + "\n" for e in lines))

    assert isinstance(_final(_meter(tmp_path, home), final=True), UnavailableRunUsage)


def test_a_truncated_trailing_record_is_the_killed_write_and_an_invalid_one_is_not(tmp_path):
    cut = _killed_child_home(tmp_path / "a", responses=2, tail='{"timestamp":"2026-10-11T04:27:1')
    bad = _killed_child_home(tmp_path / "b", responses=2, tail='{"timestamp": INVALID}')
    whole = _killed_child_home(tmp_path / "c", responses=2, tail=json.dumps(_count(CHILD_TOKENS, _scaled(3))))

    assert isinstance(_final(_meter(tmp_path / "a", cut), final=True), MeasuredRunUsage)
    assert isinstance(_final(_meter(tmp_path / "b", bad), final=True), UnavailableRunUsage)
    assert isinstance(_final(_meter(tmp_path / "c", whole), final=True), UnavailableRunUsage)


def test_recovery_of_an_unfinished_turns_run_settles_the_receipts_inside_the_run_window(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path, runtime="codex")
    _end_run_at(store, run_id, END)
    assert store.record_accounting_unavailable(run_id, UNFINISHED)
    home = _killed_child_home(tmp_path, responses=2, late_response=True)
    evidence_dir = _frozen_evidence(tmp_path, home, UNFINISHED)
    reconciler = CodexSpendReconciler(store, (str(evidence_dir),))

    assert reconciler.reconcile(run_id)
    reconciler.reconcile(run_id)  # a second sweep changes nothing

    assert store.active_reservation_runs() == ()
    assert store.snapshot(scope).settled == Money("USD", ROOT_ONLY_MICROS + 2 * CHILD_RESPONSE_MICROS)


def test_unfinished_turns_remeter_requires_the_runs_recorded_end(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    evidence_dir = _frozen_evidence(tmp_path, _killed_child_home(tmp_path), UNFINISHED)

    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs() == (run_id,)


def test_other_frozen_classes_stay_final_even_when_rollouts_would_meter(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    _end_run_at(store, run_id, END)
    evidence_dir = _frozen_evidence(tmp_path, _killed_child_home(tmp_path), "native child has no corroborating rollout")

    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs() == (run_id,)


@pytest.mark.parametrize("fault", ["absent", "later"])
def test_recovery_settles_a_receipted_root_without_a_usable_counter(tmp_path, fault):
    store, scope, run_id = _reserved_store(tmp_path, runtime="codex")
    _end_run_at(store, run_id, END)
    body = [_root_receipt(1)]
    if fault == "later":  # a later leg's counter lies beyond this run's end
        body += [_count(CHILD_TOKENS, _scaled(1)), {**_count(CHILD_TOKENS, _scaled(2)), "timestamp": _later(60)}]
    home = _root_home(tmp_path, body)
    evidence_dir = _frozen_evidence(tmp_path, home, UNFINISHED)

    assert CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)

    assert store.active_reservation_runs() == ()
    assert store.snapshot(scope).settled == Money("USD", CHILD_RESPONSE_MICROS)


def test_completed_root_turn_without_a_receipt_is_not_settled_from_the_others(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path, runtime="codex")
    _end_run_at(store, run_id, END)
    home = _root_home(tmp_path, [
        _root_receipt(1), _ev("event_msg", {"type": "task_complete", "turn_id": "t"}),
        _ev("event_msg", {"type": "task_started", "turn_id": "u"}),
        _ev("turn_context", {"turn_id": "u", "model": "gpt"}),
        _ev("event_msg", {"type": "task_complete", "turn_id": "u"}), _count(CHILD_TOKENS, _scaled(2))])

    assert isinstance(_root_sample(tmp_path, home, _counter(2)), UnavailableRunUsage)
    evidence_dir = _frozen_evidence(tmp_path, home, "")
    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs() == (run_id,)


def test_a_tail_with_a_syntax_error_before_a_cut_multibyte_character_fails_closed(tmp_path):
    store, _scope, run_id = _reserved_store(tmp_path, runtime="codex")
    _end_run_at(store, run_id, END)
    home = _killed_child_home(tmp_path, responses=2)
    with next(home.rglob(f"*{CHILD}.jsonl")).open("ab") as stream:
        stream.write(b'{"timestamp":INVALID,"note":"\xe2')
    evidence_dir = _frozen_evidence(tmp_path, home, UNFINISHED)

    assert not CodexSpendReconciler(store, (str(evidence_dir),)).reconcile(run_id)
    assert store.active_reservation_runs() == (run_id,)


def test_a_valid_prefix_ending_in_a_cut_multibyte_character_is_the_killed_write(tmp_path):
    home = _killed_child_home(tmp_path, responses=2)
    with next(home.rglob(f"*{CHILD}.jsonl")).open("ab") as stream:
        stream.write(b'{"note":"\xe2')

    assert price_run_usage(_final(_meter(tmp_path, home), final=True), CARD).micros == ROOT_ONLY_MICROS + 2 * CHILD_RESPONSE_MICROS
