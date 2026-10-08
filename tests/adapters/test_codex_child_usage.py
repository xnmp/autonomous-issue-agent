"""Native usage follows durable call/output, metadata and response contracts."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from issue_agent.adapters.codex_child_usage import CodexChildUsageMeter
from issue_agent.domain.pricing import ModelRates, RateTable
from issue_agent.domain.run_usage import MeasuredRunUsage, UnavailableRunUsage, price_run_usage, total_usage

ROOT = "019fa244-0000-7de0-8e18-e56f423bbbcc"
CHILD = "019fa244-0000-7de0-8e18-e56f423bbbcd"
OTHER = "019fa244-0000-7de0-8e18-e56f423bbbce"
NOW = datetime.now(timezone.utc)
START = NOW.timestamp() - 5
PARENT_USAGE = {"input_tokens": 1000, "output_tokens": 10, "cache_read_input_tokens": 500, "source": "rollout"}
RATES = RateTable("card", "v1", "USD", {
    "sol": ModelRates(2_000_000, 10_000_000, 100_000, 2_500_000),
    "luna": ModelRates(100_000, 500_000, 10_000, 125_000),
})


def event(kind, payload, *, at=NOW):
    return {"timestamp": at.isoformat(), "type": kind, "payload": payload}


def path(home, sid):
    directory = home / "sessions" / NOW.strftime("%Y/%m/%d")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"rollout-now-{sid}.jsonl"


def write(home, sid, records):
    path(home, sid).write_text("".join(json.dumps({**e, "ordinal": i}) + "\n" for i, e in enumerate(records)))


def metadata(sid, cwd, parent=None, *, agent_path="/root/review"):
    source = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "agent_path": agent_path}}} if parent else "exec"
    return event("session_meta", {"id": sid, "session_id": parent or sid, "source": source,
                 "originator": "codex_exec", "cwd": str(cwd), "cli_version": "0.160.0",
                 "subagent_history_start_ordinal": None})


def spawn(parent=ROOT, child=CHILD, *, v1=False, task="review"):
    args = {"message": "Review the diff."} if v1 else {"task_name": task, "message": "Review the diff."}
    output = {"agent_id": child, "nickname": None} if v1 else {"task_name": "/root/" + task, "nickname": None}
    return [
        event("response_item", {"type": "function_call", "name": "spawn_agent",
              "call_id": "call:" + child, "arguments": json.dumps(args)}),
        event("response_item", {"type": "function_call_output",
              "call_id": "call:" + child, "output": json.dumps(output)}),
    ]


def child_records(cwd, *, parent=ROOT, sid=CHILD, model="luna", complete=True, agent_path="/root/review"):
    turn = "turn:" + sid
    tokens = {"input_tokens": 1000, "cached_input_tokens": 400, "cache_write_input_tokens": 0, "output_tokens": 20}
    records = [metadata(sid, cwd, parent, agent_path=agent_path),
        event("event_msg", {"type": "task_started", "turn_id": turn}),
        event("turn_context", {"turn_id": turn, "model": model}),
        event("token_usage_record", {"thread_id": sid, "turn_id": turn,
              "response_id": "response:child", "usage": tokens, "thread_token_usage": tokens}),
        event("event_msg", {"type": "token_count", "info": {"last_token_usage": tokens, "total_token_usage": tokens}})]
    if complete:
        records.append(event("event_msg", {"type": "task_complete", "turn_id": turn}))
    return records


@pytest.fixture
def prepared(tmp_path):
    home = tmp_path / "codex"
    write(home, ROOT, [metadata(ROOT, tmp_path), *spawn()])
    write(home, CHILD, child_records(tmp_path))
    return home, CodexChildUsageMeter(str(home), START, RATES, str(tmp_path))


@pytest.mark.parametrize("v1", [False, True])
def test_both_native_agent_versions_join_one_run_at_each_models_price(prepared, v1):
    home, meter = prepared
    write(home, ROOT, [metadata(ROOT, home.parent), *spawn(v1=v1)])
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, MeasuredRunUsage)
    assert price_run_usage(observed, RATES).micros == 1224
    usage = total_usage(observed.samples)
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (1100, 30, 900)


def test_copied_fork_excludes_inherited_metadata_context_and_token_counts(prepared):
    home, meter = prepared
    child = child_records(home.parent)
    child[0]["payload"]["subagent_history_start_ordinal"] = 4
    inherited = [metadata(ROOT, home.parent),
                 event("turn_context", {"turn_id": "parent-turn", "model": "sol"}),
                 event("event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 999999999, "output_tokens": 0}}})]
    write(home, CHILD, [child[0], *inherited, *child[1:]])
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, MeasuredRunUsage)
    assert price_run_usage(observed, RATES).micros == 1224


def test_referenced_fork_does_not_read_or_charge_ancestor_history(prepared):
    home, meter = prepared
    child = child_records(home.parent)
    child[0]["payload"]["history_base"] = {"thread_id": ROOT, "end_ordinal_exclusive": 9999999, "end_byte_offset": 99999999}
    write(home, CHILD, child)
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, MeasuredRunUsage)
    assert price_run_usage(observed, RATES).micros == 1224


def test_duplicate_response_delivery_is_idempotent_across_incremental_samples(prepared):
    home, meter = prepared
    first = meter.sample(ROOT, "sol", PARENT_USAGE)
    record = child_records(home.parent)[3]
    with path(home, CHILD).open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    assert meter.sample(ROOT, "sol", PARENT_USAGE, final=True) == first


@pytest.mark.parametrize("prefix_length", [2, 3])
def test_child_startup_can_acquire_model_and_usage_between_samples(prepared, prefix_length):
    home, meter = prepared
    records = child_records(home.parent)
    write(home, CHILD, records[:prefix_length])
    for _ in range(2):
        pending = meter.sample(ROOT, "sol", PARENT_USAGE)
        assert isinstance(pending, MeasuredRunUsage)
        assert price_run_usage(pending, RATES).micros == 1150
    with path(home, CHILD).open("a") as stream:
        stream.write("".join(json.dumps(record) + "\n" for record in records[prefix_length:]))
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, MeasuredRunUsage)
    assert price_run_usage(observed, RATES).micros == 1224
    assert meter.sample(ROOT, "sol", PARENT_USAGE, final=True) == observed


def test_pending_child_model_cannot_settle_as_parent_only_usage(prepared):
    home, meter = prepared
    write(home, CHILD, child_records(home.parent)[:2])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE), MeasuredRunUsage)
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


@pytest.mark.parametrize("evidence_index", [3, 4, 5])
def test_child_usage_or_completion_without_model_fails_during_execution(prepared, evidence_index):
    home, meter = prepared
    records = child_records(home.parent)
    write(home, CHILD, [*records[:2], records[evidence_index]])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE), UnavailableRunUsage)
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


def test_explicit_unpriced_startup_model_fails_before_usage(prepared):
    home, meter = prepared
    write(home, CHILD, child_records(home.parent, model="unpriced")[:3])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE), UnavailableRunUsage)


@pytest.mark.parametrize("fault", ["unknown-model", "foreign-record", "negative", "boolean", "cache-overlap",
                                  "missing-usage", "empty-usage", "unfinished", "truncated", "unsupported-version"])
def test_incomplete_child_accounting_never_becomes_a_parent_only_receipt(prepared, fault):
    home, meter = prepared
    records = child_records(home.parent)
    if fault == "unknown-model":
        records[2]["payload"]["model"] = "unpriced"
    elif fault == "foreign-record":
        records[3]["payload"]["thread_id"] = OTHER
    elif fault in ("negative", "boolean", "cache-overlap"):
        records[3]["payload"]["usage"]["input_tokens"] = {"negative": -1, "boolean": True, "cache-overlap": 1}[fault]
    elif fault == "missing-usage":
        del records[3]
    elif fault == "empty-usage":
        records[3]["payload"]["usage"] = {}
    elif fault == "unfinished":
        records.pop()
    elif fault == "unsupported-version":
        records[0]["payload"]["cli_version"] = "0.154.0"
    write(home, CHILD, records)
    if fault == "truncated":
        with path(home, CHILD).open("a") as stream:
            stream.write('{"type":"token_usage_record"')
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


@pytest.mark.parametrize("raw", [{}, {"source": "stderr", "input_tokens": 1000, "output_tokens": 10}])
def test_missing_or_untrusted_parent_usage_cannot_become_rollout_authority(prepared, raw):
    _, meter = prepared
    assert isinstance(meter.sample(ROOT, "sol", raw, final=True), UnavailableRunUsage)


def test_same_turn_missing_response_receipt_is_detected_by_another_token_count(prepared):
    home, meter = prepared
    records = child_records(home.parent)
    records.insert(-1, records[4])
    write(home, CHILD, records)
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


@pytest.mark.parametrize("complete", [False, True])
def test_prior_completed_turn_does_not_cover_followup_without_usage(prepared, complete):
    home, meter = prepared
    records = child_records(home.parent)
    records.extend([event("event_msg", {"type": "task_started", "turn_id": "new-turn"}),
                    event("turn_context", {"turn_id": "new-turn", "model": "luna"})])
    if complete:
        records.append(event("event_msg", {"type": "task_complete", "turn_id": "new-turn"}))
    write(home, CHILD, records)
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


def test_unwitnessed_sibling_is_not_omitted_when_another_child_is_valid(prepared):
    home, meter = prepared
    write(home, OTHER, child_records(home.parent, sid=OTHER, agent_path="/root/other"))
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


def test_failure_stays_unavailable_after_incremental_offset_advances(prepared):
    home, meter = prepared
    with path(home, ROOT).open("a") as stream:
        stream.write("{invalid json}\n")
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE), UnavailableRunUsage)
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


def test_partial_header_can_complete_before_final_settlement(prepared):
    home, meter = prepared
    encoded = path(home, CHILD).read_text()
    path(home, CHILD).write_text(encoded[:20])
    partial = meter.sample(ROOT, "sol", PARENT_USAGE)
    assert isinstance(partial, MeasuredRunUsage)
    assert price_run_usage(partial, RATES).micros == 1150
    with path(home, CHILD).open("a") as stream:
        stream.write(encoded[20:])
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, MeasuredRunUsage)
    assert price_run_usage(observed, RATES).micros == 1224


@pytest.mark.parametrize("bound,value", [("max_children", 0), ("max_files", 1), ("max_bytes", 32),
                                      ("max_record_bytes", 32), ("max_responses", 0)])
def test_observer_limits_fail_closed(prepared, bound, value):
    _, meter = prepared
    observed = replace(meter, **{bound: value}).sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, UnavailableRunUsage)


def test_child_cannot_borrow_a_foreign_parent_receipt(prepared):
    home, meter = prepared
    write(home, CHILD, child_records(home.parent, parent=OTHER))
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


def test_successful_spawn_without_child_metadata_does_not_infer_zero_usage(prepared):
    home, meter = prepared
    path(home, CHILD).unlink()
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


def test_unwitnessed_child_cannot_hide_behind_root_only_accounting(prepared):
    home, meter = prepared
    write(home, ROOT, [metadata(ROOT, home.parent)])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


def test_restart_reconstructs_same_response_receipt_from_immutable_start(prepared):
    home, meter = prepared
    first = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    restarted = CodexChildUsageMeter(str(home), START, RATES, str(home.parent))
    assert restarted.sample(ROOT, "sol", PARENT_USAGE, final=True) == first


def test_root_only_and_unrelated_session_preserve_existing_accounting(prepared):
    home, meter = prepared
    write(home, ROOT, [metadata(ROOT, home.parent)])
    write(home, CHILD, child_records(home.parent, parent=OTHER))
    result = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(result, MeasuredRunUsage)
    assert price_run_usage(result, RATES).micros == 1150
    legacy = replace(meter, rates=None)
    assert legacy.sample(ROOT, "sol", PARENT_USAGE, final=True) is None


def test_owned_child_can_spawn_an_accounted_descendant(prepared):
    home, meter = prepared
    records = child_records(home.parent) + spawn(CHILD, OTHER, task="nested")
    records[-1]["payload"]["output"] = json.dumps({"task_name": "/root/review/nested", "nickname": None})
    write(home, CHILD, records)
    write(home, OTHER, child_records(home.parent, parent=CHILD, sid=OTHER, agent_path="/root/review/nested"))
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, MeasuredRunUsage)
    assert price_run_usage(observed, RATES).micros == 1298


def test_estimator_shaped_update_cannot_hide_missing_paid_response(prepared):
    home, meter = prepared
    records = child_records(home.parent)
    records.insert(3, event("event_msg", {"type": "token_count", "info": {"last_token_usage": {
        "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "total_tokens": 12000}}}))
    write(home, CHILD, records)
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


def test_repeated_estimator_after_missing_response_is_not_a_zero_charge(prepared):
    home, meter = prepared
    records = child_records(home.parent)
    estimator = event("event_msg", {"type": "token_count", "info": {"last_token_usage": {
        "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
        "cache_write_input_tokens": 0, "total_tokens": 12000}}})
    write(home, CHILD, [*records[:3], estimator, estimator, records[-1]])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


@pytest.mark.parametrize("name,target", [("followup_task", "/root/review"), ("send_message", "/root/review"),
                                        ("send_input", CHILD), ("resume_agent", CHILD)])
def test_current_followup_to_prior_child_keeps_obligation_without_its_baseline(prepared, name, target):
    home, meter = prepared
    prior = NOW - timedelta(minutes=10)
    old_spawn = [{**record, "timestamp": prior.isoformat()} for record in spawn()]
    call = event("response_item", {"type": "function_call", "name": name,
                 "call_id": "followup", "arguments": json.dumps({"target": target, "message": "Continue"})})
    write(home, ROOT, [metadata(ROOT, home.parent), *old_spawn, call])
    records = child_records(home.parent)
    records[0]["timestamp"] = prior.isoformat()
    write(home, CHILD, records)
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


def test_relative_followup_reference_joins_corroborated_current_child(prepared):
    home, meter = prepared
    call = event("response_item", {"type": "function_call", "name": "followup_task", "call_id": "followup",
                 "arguments": json.dumps({"target": "review", "message": "Clarify"})})
    write(home, ROOT, [metadata(ROOT, home.parent), *spawn(), call])
    result = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(result, MeasuredRunUsage)
    assert price_run_usage(result, RATES).micros == 1224


@pytest.mark.parametrize("target", [ROOT, "/root"])
def test_child_message_to_current_parent_preserves_complete_mixed_usage(prepared, target):
    home, meter = prepared
    call = event("response_item", {"type": "function_call", "name": "send_message", "call_id": "report",
                 "arguments": json.dumps({"target": target, "message": "Review complete"})})
    write(home, CHILD, [*child_records(home.parent), call])
    result = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(result, MeasuredRunUsage)
    assert price_run_usage(result, RATES).micros == 1224


@pytest.mark.parametrize("operation", ["followup_task", "send_input", "resume_agent"])
def test_child_cannot_activate_its_parent_as_an_unaccounted_followup(prepared, operation):
    home, meter = prepared
    call = event("response_item", {"type": "function_call", "name": operation, "call_id": "activate",
                 "arguments": json.dumps({"target": "/root", "message": "Continue"})})
    write(home, CHILD, [*child_records(home.parent), call])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


@pytest.mark.parametrize("target", [OTHER, "/root/foreign"])
def test_message_cannot_borrow_an_unwitnessed_recipients_usage_baseline(prepared, target):
    home, meter = prepared
    call = event("response_item", {"type": "function_call", "name": "send_message", "call_id": "foreign",
                 "arguments": json.dumps({"target": target, "message": "Review"})})
    write(home, CHILD, [*child_records(home.parent), call])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


@pytest.mark.parametrize("target", [OTHER, "/root/other"])
def test_messages_between_current_siblings_preserve_both_usage_receipts(prepared, target):
    home, meter = prepared
    write(home, ROOT, [metadata(ROOT, home.parent), *spawn(), *spawn(child=OTHER, task="other")])
    call = event("response_item", {"type": "function_call", "name": "send_message", "call_id": "peer",
                 "arguments": json.dumps({"target": target, "message": "Compare findings"})})
    write(home, CHILD, [*child_records(home.parent), call])
    write(home, OTHER, child_records(home.parent, sid=OTHER, agent_path="/root/other"))
    result = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(result, MeasuredRunUsage)
    assert price_run_usage(result, RATES).micros == 1298


def test_parent_notification_does_not_complete_the_childs_usage(prepared):
    home, meter = prepared
    call = event("response_item", {"type": "function_call", "name": "send_message", "call_id": "unfinished",
                 "arguments": json.dumps({"target": "/root", "message": "Still reviewing"})})
    write(home, CHILD, [*child_records(home.parent, complete=False), call])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


@pytest.mark.parametrize("reverse", [False, True])
def test_notification_cannot_hide_activation_of_the_same_recipient(prepared, reverse):
    home, meter = prepared
    calls = [event("response_item", {"type": "function_call", "name": operation, "call_id": operation,
                   "arguments": json.dumps({"target": "/root", "message": "Continue"})})
             for operation in ("send_message", "followup_task")]
    write(home, CHILD, [*child_records(home.parent), *(reversed(calls) if reverse else calls)])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), UnavailableRunUsage)


@pytest.mark.parametrize("complete", [False, True])
def test_notification_during_live_usage_needs_completion_before_final_receipt(prepared, complete):
    home, meter = prepared
    records = child_records(home.parent, complete=False)
    records.append(event("response_item", {"type": "function_call", "name": "send_message", "call_id": "live",
                         "arguments": json.dumps({"target": "/root", "message": "Reviewing"})}))
    write(home, CHILD, records)
    streamed = meter.sample(ROOT, "sol", PARENT_USAGE)
    assert isinstance(streamed, MeasuredRunUsage)
    assert price_run_usage(streamed, RATES).micros == 1224
    if complete:
        with path(home, CHILD).open("a") as stream:
            stream.write(json.dumps(event("event_msg", {"type": "task_complete", "turn_id": "turn:" + CHILD})) + "\n")
    final = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(final, MeasuredRunUsage if complete else UnavailableRunUsage)
