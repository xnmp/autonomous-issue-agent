"""Native usage follows durable call/output, metadata and response contracts."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from issue_agent.adapters.codex_child_usage import CodexChildUsageMeter
from issue_agent.domain.pricing import ModelRates, RateTable
from issue_agent.domain.run_usage import MeasuredRunUsage, EstimatedRunUsage, price_run_usage, total_usage

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


def _count(last, total, *, rate_limit=0):
    info = {"last_token_usage": last, "total_token_usage": total}
    return event("event_msg", {"type": "token_count", "info": info, "rate_limits": {"used": rate_limit}})


def _settled_tokens(meter):
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    return observed, (total_usage(observed.samples) if isinstance(observed, (MeasuredRunUsage, EstimatedRunUsage)) else None)


def test_reemitted_token_count_with_identical_totals_is_not_double_counted(prepared):
    home, meter = prepared
    records = child_records(home.parent)
    records.insert(-1, _count(records[4]["payload"]["info"]["last_token_usage"],
                              records[4]["payload"]["info"]["total_token_usage"], rate_limit=7))
    write(home, CHILD, records)
    observed, usage = _settled_tokens(meter)
    assert isinstance(observed, (MeasuredRunUsage, EstimatedRunUsage))
    assert price_run_usage(observed, RATES).micros == 1224
    assert (usage.input_tokens, usage.output_tokens) == (1100, 30)


def test_reemission_split_across_incremental_samples_is_still_recognised(prepared):
    home, meter = prepared
    records = child_records(home.parent, complete=False)
    write(home, CHILD, records)
    assert price_run_usage(meter.sample(ROOT, "sol", PARENT_USAGE), RATES).micros == 1224
    tokens = records[4]["payload"]["info"]
    records += [_count(tokens["last_token_usage"], tokens["total_token_usage"]),
                event("event_msg", {"type": "task_complete", "turn_id": "turn:" + CHILD})]
    write(home, CHILD, records)
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


def test_many_reemissions_do_not_consume_the_token_count_bound(prepared):
    home, _ = prepared
    meter = CodexChildUsageMeter(str(home), START, RATES, str(home.parent), max_responses=8)
    records = child_records(home.parent)
    info = records[4]["payload"]["info"]
    records[5:5] = [_count(info["last_token_usage"], info["total_token_usage"], rate_limit=i) for i in range(500)]
    write(home, CHILD, records)
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


@pytest.mark.parametrize("complete", [False, True])
def test_prior_completed_turn_does_not_cover_followup_without_usage(prepared, complete):
    home, meter = prepared
    records = child_records(home.parent)
    records.extend([event("event_msg", {"type": "task_started", "turn_id": "new-turn"}),
                    event("turn_context", {"turn_id": "new-turn", "model": "luna"})])
    if complete:
        records.append(event("event_msg", {"type": "task_complete", "turn_id": "new-turn"}))
    write(home, CHILD, records)
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    # Missing follow-up usage contributes zero without discarding earlier requests.
    if complete:
        assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 1224


def test_unwitnessed_sibling_is_not_omitted_when_another_child_is_valid(prepared):
    home, meter = prepared
    write(home, OTHER, child_records(home.parent, sid=OTHER, agent_path="/root/other"))
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


def test_estimate_gap_survives_incremental_offset_advances(prepared):
    home, meter = prepared
    with path(home, ROOT).open("a") as stream:
        stream.write("{invalid json}\n")
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE), EstimatedRunUsage)
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


def test_partial_header_can_complete_before_final_settlement(prepared):
    home, meter = prepared
    encoded = path(home, CHILD).read_text()
    path(home, CHILD).write_text(encoded[:20])
    partial = meter.sample(ROOT, "sol", PARENT_USAGE)
    assert isinstance(partial, EstimatedRunUsage)
    assert price_run_usage(partial, RATES).micros == 1150
    with path(home, CHILD).open("a") as stream:
        stream.write(encoded[20:])
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, (MeasuredRunUsage, EstimatedRunUsage))
    assert price_run_usage(observed, RATES).micros == 1224


@pytest.mark.parametrize("bound,value", [("max_children", 0), ("max_files", 1), ("max_bytes", 32),
                                      ("max_record_bytes", 32), ("max_responses", 0)])
def test_terminal_observer_limits_settle_best_estimate(prepared, bound, value):
    _, meter = prepared
    observed = replace(meter, **{bound: value}).sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, EstimatedRunUsage)


@pytest.mark.parametrize("receipt_counter,first_count", [(False, True), (False, False), (True, True), (True, False)])
def test_root_mixed_receipt_and_cumulative_only_requests_use_all_evidence(tmp_path, receipt_counter, first_count):
    home = tmp_path / "codex"
    tokens = {"input_tokens": 100, "output_tokens": 0}
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": "r1", "usage": tokens,
                                     **({"thread_token_usage": tokens} if receipt_counter else {})}),
        *([_count(None, tokens)] if first_count else []), _count(None, {"input_tokens": 200, "output_tokens": 0})])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 400


@pytest.mark.parametrize("matched_stream", [False, True])
def test_receipt_does_not_disable_remaining_root_aggregate_evidence(tmp_path, matched_stream):
    home = tmp_path / "codex"
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": "r1",
                                     "usage": {"input_tokens": 100, "output_tokens": 0}}),
        *([_count({"input_tokens": 100, "output_tokens": 0}, {"input_tokens": 100, "output_tokens": 0})] if matched_stream else [])])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol",
        {"input_tokens": 200, "output_tokens": 0, "source": "rollout"}, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 400


def test_legacy_receipt_preceding_linked_receipt_is_not_subtracted_from_the_next_cumulative_request(tmp_path):
    home = tmp_path / "codex"
    tokens = {"input_tokens": 100, "output_tokens": 0}
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": "r1", "usage": tokens}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": "r2", "usage": tokens,
                                     "thread_token_usage": {"input_tokens": 200, "output_tokens": 0}}),
        _count(None, {"input_tokens": 300, "output_tokens": 0})])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert price_run_usage(observed, RATES).micros == 600


@pytest.mark.parametrize("linked_second", [False, True])
def test_receipt_covered_by_cumulative_evidence_cannot_hide_a_later_equal_request(tmp_path, linked_second):
    home = tmp_path / "codex"
    tokens = {"input_tokens": 100, "output_tokens": 0}
    write(home, ROOT, [metadata(ROOT, tmp_path), *spawn()])
    write(home, CHILD, [metadata(CHILD, tmp_path, ROOT), event("turn_context", {"turn_id": "t", "model": "sol"}),
        event("token_usage_record", {"thread_id": CHILD, "turn_id": "t", "response_id": "r1", "usage": tokens}),
        *([event("token_usage_record", {"thread_id": CHILD, "turn_id": "t", "response_id": "r2", "usage": tokens,
                                      "thread_token_usage": {"input_tokens": 200, "output_tokens": 0}})] if linked_second else
          [_count(None, tokens)]),
        _count(tokens, {"input_tokens": 300 if linked_second else 200, "output_tokens": 0})])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == (600 if linked_second else 400)


def test_malformed_root_aggregate_keeps_valid_request_receipts(tmp_path):
    home = tmp_path / "codex"
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": "r1",
                                     "usage": {"input_tokens": 100, "output_tokens": 0}})])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", {"bad": "usage"}, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 200


def test_fully_receipted_child_cannot_hide_absent_parent_request_usage(prepared):
    _, meter = prepared
    observed = meter.sample(ROOT, "sol", None, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 74


def test_previous_leg_missing_turn_is_outside_this_runs_gap_obligations(tmp_path):
    home = tmp_path / "codex"
    prior = NOW - timedelta(minutes=10)
    write(home, ROOT, [metadata(ROOT, tmp_path),
        event("event_msg", {"type": "task_complete", "turn_id": "prior-missing"}, at=prior),
        event("turn_context", {"turn_id": "current", "model": "sol"}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "current", "response_id": "r",
                                     "usage": {"input_tokens": 100, "output_tokens": 0}})])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert isinstance(observed, MeasuredRunUsage)
    assert price_run_usage(observed, RATES).micros == 200


def test_child_copied_ancestor_history_without_ordinal_boundary_is_excluded(tmp_path):
    home = tmp_path / "codex"
    tokens = {"input_tokens": 100, "output_tokens": 0}
    inherited = [event("turn_context", {"turn_id": "parent-turn", "model": "sol"}), _count(tokens, tokens)]
    write(home, ROOT, [metadata(ROOT, tmp_path), *inherited, *spawn()])
    write(home, CHILD, [metadata(CHILD, tmp_path, ROOT), *inherited, *child_records(tmp_path)[1:]])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert price_run_usage(observed, RATES).micros == 274


def test_receipted_root_followed_by_completed_unusaged_request_is_an_estimate(tmp_path):
    home = tmp_path / "codex"
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "paid", "model": "sol"}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "paid", "response_id": "r1",
                                     "usage": {"input_tokens": 100, "output_tokens": 0}}),
        event("event_msg", {"type": "task_complete", "turn_id": "paid"}),
        event("event_msg", {"type": "task_started", "turn_id": "missing"}),
        event("event_msg", {"type": "task_complete", "turn_id": "missing"})])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 200


@pytest.mark.parametrize("v1", [False, True])
def test_shared_home_with_2000_unrelated_rollouts_keeps_owned_usage(prepared, v1):
    home, meter = prepared
    write(home, ROOT, [metadata(ROOT, home.parent), *spawn(v1=v1)])
    for i in range(2000):
        sid = f"019fa244-0000-7de0-8e18-{i:012x}"
        write(home, sid, [metadata(sid, home.parent, parent=OTHER)])
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 1224


def test_pathological_shared_directory_fails_closed(prepared):
    home, meter = prepared
    directory = path(home, ROOT).parent
    for i in range(65534):
        (directory / f"unrelated-{i}").touch()
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)
    (directory / "one-too-many").touch()
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert observed.detail == "native rollout discovery exceeded its scan bound"


def test_non_rollout_entries_do_not_consume_owned_file_bound(prepared):
    home, meter = prepared
    for i in range(513):
        (path(home, ROOT).parent / f"unrelated-{i}").touch()
    observed = replace(meter, max_files=2).sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 1224


def test_child_cannot_borrow_a_foreign_parent_receipt(prepared):
    home, meter = prepared
    write(home, CHILD, child_records(home.parent, parent=OTHER))
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


def test_successful_spawn_without_child_metadata_does_not_infer_zero_usage(prepared):
    home, meter = prepared
    path(home, CHILD).unlink()
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


def test_unwitnessed_child_cannot_hide_behind_root_only_accounting(prepared):
    home, meter = prepared
    write(home, ROOT, [metadata(ROOT, home.parent)])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


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
    assert isinstance(result, EstimatedRunUsage)
    assert price_run_usage(result, RATES).micros == 1150
    legacy = replace(meter, rates=None)
    assert price_run_usage(legacy.sample(ROOT, "sol", PARENT_USAGE, final=True), RATES).micros == 0


def test_owned_child_can_spawn_an_accounted_descendant(prepared):
    home, meter = prepared
    records = child_records(home.parent) + spawn(CHILD, OTHER, task="nested")
    records[-1]["payload"]["output"] = json.dumps({"task_name": "/root/review/nested", "nickname": None})
    write(home, CHILD, records)
    write(home, OTHER, child_records(home.parent, parent=CHILD, sid=OTHER, agent_path="/root/review/nested"))
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, (MeasuredRunUsage, EstimatedRunUsage))
    assert price_run_usage(observed, RATES).micros == 1298


def test_repeated_estimator_after_missing_response_is_not_a_zero_charge(prepared):
    home, meter = prepared
    records = child_records(home.parent)
    estimator = event("event_msg", {"type": "token_count", "info": {"last_token_usage": {
        "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
        "cache_write_input_tokens": 0, "total_tokens": 12000}}})
    write(home, CHILD, [*records[:3], estimator, estimator, records[-1]])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


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
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


def test_relative_followup_reference_joins_corroborated_current_child(prepared):
    home, meter = prepared
    call = event("response_item", {"type": "function_call", "name": "followup_task", "call_id": "followup",
                 "arguments": json.dumps({"target": "review", "message": "Clarify"})})
    write(home, ROOT, [metadata(ROOT, home.parent), *spawn(), call])
    result = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(result, EstimatedRunUsage)
    assert price_run_usage(result, RATES).micros == 1224


@pytest.mark.parametrize("target", [ROOT, "/root"])
def test_child_message_to_current_parent_preserves_complete_mixed_usage(prepared, target):
    home, meter = prepared
    call = event("response_item", {"type": "function_call", "name": "send_message", "call_id": "report",
                 "arguments": json.dumps({"target": target, "message": "Review complete"})})
    write(home, CHILD, [*child_records(home.parent), call])
    result = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(result, EstimatedRunUsage)
    assert price_run_usage(result, RATES).micros == 1224


@pytest.mark.parametrize("operation", ["followup_task", "send_input", "resume_agent"])
def test_child_cannot_activate_its_parent_as_an_unaccounted_followup(prepared, operation):
    home, meter = prepared
    call = event("response_item", {"type": "function_call", "name": operation, "call_id": "activate",
                 "arguments": json.dumps({"target": "/root", "message": "Continue"})})
    write(home, CHILD, [*child_records(home.parent), call])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


@pytest.mark.parametrize("target", [OTHER, "/root/foreign"])
def test_message_cannot_borrow_an_unwitnessed_recipients_usage_baseline(prepared, target):
    home, meter = prepared
    call = event("response_item", {"type": "function_call", "name": "send_message", "call_id": "foreign",
                 "arguments": json.dumps({"target": target, "message": "Review"})})
    write(home, CHILD, [*child_records(home.parent), call])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


@pytest.mark.parametrize("target", [OTHER, "/root/other"])
def test_messages_between_current_siblings_preserve_both_usage_receipts(prepared, target):
    home, meter = prepared
    write(home, ROOT, [metadata(ROOT, home.parent), *spawn(), *spawn(child=OTHER, task="other")])
    call = event("response_item", {"type": "function_call", "name": "send_message", "call_id": "peer",
                 "arguments": json.dumps({"target": target, "message": "Compare findings"})})
    write(home, CHILD, [*child_records(home.parent), call])
    write(home, OTHER, child_records(home.parent, sid=OTHER, agent_path="/root/other"))
    result = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(result, EstimatedRunUsage)
    assert price_run_usage(result, RATES).micros == 1298


def test_parent_notification_does_not_complete_the_childs_usage(prepared):
    home, meter = prepared
    call = event("response_item", {"type": "function_call", "name": "send_message", "call_id": "unfinished",
                 "arguments": json.dumps({"target": "/root", "message": "Still reviewing"})})
    write(home, CHILD, [*child_records(home.parent, complete=False), call])
    assert price_run_usage(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), RATES).micros == 1224


@pytest.mark.parametrize("reverse", [False, True])
def test_notification_cannot_hide_activation_of_the_same_recipient(prepared, reverse):
    home, meter = prepared
    calls = [event("response_item", {"type": "function_call", "name": operation, "call_id": operation,
                   "arguments": json.dumps({"target": "/root", "message": "Continue"})})
             for operation in ("send_message", "followup_task")]
    write(home, CHILD, [*child_records(home.parent), *(reversed(calls) if reverse else calls)])
    assert isinstance(meter.sample(ROOT, "sol", PARENT_USAGE, final=True), EstimatedRunUsage)


@pytest.mark.parametrize("complete", [False, True])
def test_notification_during_live_usage_needs_completion_before_final_receipt(prepared, complete):
    home, meter = prepared
    records = child_records(home.parent, complete=False)
    records.append(event("response_item", {"type": "function_call", "name": "send_message", "call_id": "live",
                         "arguments": json.dumps({"target": "/root", "message": "Reviewing"})}))
    write(home, CHILD, records)
    streamed = meter.sample(ROOT, "sol", PARENT_USAGE)
    assert isinstance(streamed, EstimatedRunUsage)
    assert price_run_usage(streamed, RATES).micros == 1224
    if complete:
        with path(home, CHILD).open("a") as stream:
            stream.write(json.dumps(event("event_msg", {"type": "task_complete", "turn_id": "turn:" + CHILD})) + "\n")
    final = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(final, EstimatedRunUsage)


def test_receipt_cumulative_identity_wins_over_divergent_stream_and_keeps_the_next_request(tmp_path):
    home = tmp_path / "codex"
    receipt = {"input_tokens": 100, "output_tokens": 0}
    divergent = {"input_tokens": 90, "output_tokens": 0}
    total = {"input_tokens": 200, "output_tokens": 0}
    write(home, ROOT, [metadata(ROOT, tmp_path),
        event("turn_context", {"turn_id": "t", "model": "sol"}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": "r1",
                                     "usage": receipt, "thread_token_usage": receipt}),
        _count(divergent, receipt), _count(receipt, total), _count(receipt, total)])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 400


def test_malformed_cumulative_metadata_does_not_discard_receipt_usage(tmp_path):
    home = tmp_path / "codex"
    write(home, ROOT, [metadata(ROOT, tmp_path),
        event("turn_context", {"turn_id": "t", "model": "sol"}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": "r1",
            "usage": {"input_tokens": 100, "output_tokens": 0}, "thread_token_usage": {"input_tokens": -1}})])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 200


def test_malformed_request_does_not_discard_receipts_before_or_after_it(tmp_path):
    home = tmp_path / "codex"
    records = [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"})]
    for response, inputs in (("r1", 100), ("bad", 10**100), ("r2", 200)):
        records.append(event("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": response,
            "usage": {"input_tokens": inputs, "output_tokens": 0}}))
    write(home, ROOT, records)
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 600


def test_mixed_legacy_and_cumulative_receipts_do_not_consume_each_others_streams(tmp_path):
    home = tmp_path / "codex"
    tokens = {"input_tokens": 100, "output_tokens": 0}
    total = {"input_tokens": 200, "output_tokens": 0}
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": "r1",
                                     "usage": tokens, "thread_token_usage": tokens}), _count(tokens, tokens),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": "r2", "usage": tokens}),
        _count(tokens, total)])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    assert price_run_usage(observed, RATES).micros == 400


def test_child_with_only_cumulative_streamed_usage_is_priced_as_an_estimate(prepared):
    home, meter = prepared
    records = [r for r in child_records(home.parent) if r["type"] != "token_usage_record"]
    next(r for r in records if r["type"] == "event_msg" and r["payload"]["type"] == "token_count")["payload"]["info"].pop("last_token_usage")
    write(home, CHILD, records)
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 1224


@pytest.mark.parametrize("payload", [{"source": {"subagent": "malformed"}}, ["malformed"], "malformed"])
def test_unrelated_malformed_metadata_does_not_discard_owned_child_receipts(prepared, payload):
    home, meter = prepared
    record = metadata(OTHER, home.parent)
    record["payload"] = payload
    write(home, OTHER, [record])
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert price_run_usage(observed, RATES).micros == 1224


def test_malformed_owned_child_metadata_keeps_the_parent_charge(prepared):
    home, meter = prepared
    write(home, ROOT, [metadata(ROOT, home.parent), *spawn(v1=True)])
    records = child_records(home.parent)
    records[0]["payload"]["source"] = {"subagent": "malformed"}
    write(home, CHILD, records)
    observed = meter.sample(ROOT, "sol", PARENT_USAGE, final=True)
    assert isinstance(observed, EstimatedRunUsage)
    assert price_run_usage(observed, RATES).micros == 1150


def test_receipt_only_roots_from_every_transport_attempt_remain_in_the_leg(tmp_path):
    home = tmp_path / "codex"
    for sid in (ROOT, OTHER):
        write(home, sid, [metadata(sid, tmp_path), event("turn_context", {"turn_id": "t", "model": "sol"}),
            event("token_usage_record", {"thread_id": sid, "turn_id": "t", "response_id": "r",
                                         "usage": {"input_tokens": 100, "output_tokens": 0}})])
    meter = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path))
    assert price_run_usage(meter.sample(ROOT, "sol", None), RATES).micros == 200
    assert price_run_usage(meter.sample(OTHER, "sol", None, final=True), RATES).micros == 400
