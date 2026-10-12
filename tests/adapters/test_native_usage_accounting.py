"""One reservation settles root and native review usage exactly once."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.contracts import Money, TokenUsage, UsageSource
from issue_agent.domain.outcome import AgentResult, ExitReason, infra_class_failure
from issue_agent.domain.pricing import BudgetPolicy, RateTable, ModelRates
from issue_agent.domain.run_usage import ModelUsage, MeasuredRunUsage, EstimatedRunUsage
from tests.adapters.test_accounting_runtime import Runtime, _wrapper, _publication_context_for_accounting_tests
from tests.fakes.workflow_authority import resolve_snapshot
from issue_agent.adapters.codex_child_usage import rate_table_evidence
from issue_agent.adapters.codex_runner import CodexRunner, write_spend_evidence, spend_evidence_path
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.spend_recovery import CodexSpendReconciler
from issue_agent.adapters.control_store import AccountingDisposition
from tests.adapters.test_spend_recovery import _end_run_at, _reserved_store

ENDED = datetime(2100, 1, 1, tzinfo=timezone.utc)
from tests.adapters.test_codex_child_usage import (
    ROOT, CHILD, OTHER, NOW, START, RATES, metadata, spawn, child_records, event, write,
)


def samples():
    return (
        ModelUsage("parent", "sol", TokenUsage(500, 10, 500, source=UsageSource.ROLLOUT)),
        ModelUsage("review", "luna", TokenUsage(600, 20, 400, source=UsageSource.ROLLOUT)),
    )


def table():
    return RateTable("native-card", "v1", "USD", {
        "sol": ModelRates(2000000, 10000000, 100000, 2500000),
        "luna": ModelRates(100000, 500000, 10000, 125000),
    })


def test_same_admission_prices_each_native_model_and_emits_one_aggregate_charge(tmp_path):
    result = AgentResult(False, status="pushed", session_id="parent",
                         usage={"input_tokens": 1000, "output_tokens": 10,
                                "cache_read_input_tokens": 500, "source": "rollout"},
                         run_usage=MeasuredRunUsage(samples()))
    wrapper, control = _wrapper(tmp_path, Runtime(result))
    wrapper = replace(wrapper, rate_table=table())
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    asyncio.run(wrapper.run(RuntimePlan("work", model="sol", max_budget_usd=2)))
    with control.transaction() as db:
        charges = db.execute("SELECT amount_micros,basis FROM charges").fetchall()
        counts = {name: db.execute("SELECT count(*) FROM " + name).fetchone()[0]
                  for name in ("runs", "budget_reservations", "charges")}
    assert [(r["amount_micros"], r["basis"]) for r in charges] == [(1224, "configured_rate_card")]
    assert counts == {"runs": 1, "budget_reservations": 1, "charges": 1}
    assert control.active_reservation_runs() == ()


def test_missing_child_receipt_settles_known_parent_estimate_without_holding(tmp_path):
    result = AgentResult(False, status="pushed", session_id="parent",
                         run_usage=EstimatedRunUsage("child receipt missing", samples()[:1]))
    wrapper, control = _wrapper(tmp_path, Runtime(result))
    wrapper = replace(wrapper, rate_table=table())
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    returned = asyncio.run(wrapper.run(RuntimePlan("work", model="sol", max_budget_usd=2)))
    assert returned.status == "pushed" and not infra_class_failure(returned)
    assert control.active_reservation_runs() == ()
    with control.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (1150, "estimate")


def test_interrupted_unusaged_native_request_settles_zero_without_the_reserved_maximum(tmp_path):
    result = AgentResult(True, exit_reason=ExitReason.NO_RESULT,
                         run_usage=EstimatedRunUsage("interrupted request"))
    wrapper, control = _wrapper(tmp_path, Runtime(result))
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    asyncio.run(wrapper.run(RuntimePlan("work", model="sol", max_budget_usd=2)))
    assert control.active_reservation_runs() == ()
    with control.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")


@pytest.mark.parametrize("crash_after_commit", [False, True])
def test_completed_request_without_evidence_journals_gap_in_actual_settlement(tmp_path, monkeypatch, crash_after_commit):
    from issue_agent.adapters.codex_child_usage import CodexChildUsageMeter
    home = tmp_path / "codex"
    write(home, ROOT, [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "paid", "model": "sol"}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "paid", "response_id": "r1",
                                     "usage": {"input_tokens": 100, "output_tokens": 0}}),
        event("event_msg", {"type": "task_complete", "turn_id": "paid"}),
        event("event_msg", {"type": "task_complete", "turn_id": "missing"})])
    observed = CodexChildUsageMeter(str(home), START, RATES, str(tmp_path)).sample(ROOT, "sol", None, final=True)
    wrapper, control = _wrapper(tmp_path, Runtime(AgentResult(False, status="pushed", run_usage=observed)))
    wrapper = replace(wrapper, rate_table=RATES)
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    if crash_after_commit:
        settle = control.settle

        def committed_then_crash(request):
            settle(request)
            raise OSError("caller disappeared after settlement commit")

        monkeypatch.setattr(control, "settle", committed_then_crash)
        with pytest.raises(OSError, match="caller disappeared"):
            asyncio.run(wrapper.run(RuntimePlan("work", model="sol", max_budget_usd=2)))
    else:
        asyncio.run(wrapper.run(RuntimePlan("work", model="sol", max_budget_usd=2)))
    with control.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (200, "estimate")


def test_recovery_excludes_copied_history_without_ordinal_boundary_and_replays_once(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path)
    home, evidence = recovery_files(tmp_path)
    tokens = {"input_tokens": 100, "output_tokens": 0}
    inherited = [event("turn_context", {"turn_id": "parent-turn", "model": "sol"}),
                 event("event_msg", {"type": "token_count", "info": {"last_token_usage": tokens, "total_token_usage": tokens}})]
    write(home, ROOT, [metadata(ROOT, tmp_path), *inherited, *spawn()])
    write(home, CHILD, [metadata(CHILD, tmp_path, ROOT), *inherited, *child_records(tmp_path)[1:]])
    directory = tmp_path / "evidence"
    write_spend_evidence(str(directory), str(run_id), evidence)
    _end_run_at(store, run_id, ENDED)
    reconciler = CodexSpendReconciler(store, (str(directory),))
    assert reconciler.reconcile(run_id)
    assert not reconciler.reconcile(run_id)
    assert store.snapshot(scope).settled == Money("USD", 274)
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 1


def recovery_files(tmp_path):
    home = tmp_path / "codex"
    parent_tokens = {"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 10}
    root_records = [metadata(ROOT, tmp_path), *spawn(),
                    event("event_msg", {"type": "token_count", "info": {"total_token_usage": parent_tokens}})]
    write(home, ROOT, root_records)
    write(home, CHILD, child_records(tmp_path))
    evidence = {
        "cwd": str(tmp_path), "rollout_started_at": START, "session_id": ROOT,
        "baseline_session_id": "", "baseline_usage": None, "model": "sol",
        "cost_per_mtok_in": 2, "cost_per_mtok_out": 10, "cost_per_mtok_cached_in": 0.1,
        "codex_home": str(home), "config_version": "cfg", "rate_card_id": "card", "rate_card_version": "v1",
        "native_usage_started_at": START, "native_root_sessions": {ROOT: "sol"},
        "native_rate_table": rate_table_evidence(RATES), "native_usage_failure": "",
    }
    return home, evidence


@pytest.mark.parametrize("fork", ["fresh", "copied", "referenced"])
def test_native_recovery_settles_exact_frozen_mixed_price_once(tmp_path, fork):
    store, scope, run_id = _reserved_store(tmp_path)
    home, evidence = recovery_files(tmp_path)
    child = child_records(tmp_path)
    if fork == "copied":
        child[0]["payload"]["subagent_history_start_ordinal"] = 3
        child = [child[0], metadata(ROOT, tmp_path), event("event_msg", {"type": "token_count", "info": {}}), *child[1:]]
    elif fork == "referenced":
        child[0]["payload"]["history_base"] = {"thread_id": ROOT, "end_ordinal_exclusive": 999, "end_byte_offset": 99999}
    write(home, CHILD, child)
    directory = tmp_path / "evidence"
    write_spend_evidence(str(directory), str(run_id), evidence)
    _end_run_at(store, run_id, ENDED)
    reconciler = CodexSpendReconciler(store, (str(directory),))
    assert reconciler.reconcile(run_id)
    assert not reconciler.reconcile(run_id)
    assert store.snapshot(scope).settled == Money("USD", 1224)
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 1


@pytest.mark.parametrize("fault", ["native_usage_started_at", "native_root_sessions", "native_rate_table",
                                  "native_usage_failure", "baseline_session_id", "unsupported-version",
                                  "boolean-version", "failure-shape", "invalid-card", "unfinished-child",
                                  "sticky-failure", "huge-start", "wrong-root-model", "currency-conflict"])
def test_malformed_or_incomplete_native_recovery_never_downgrades_to_parent_only(tmp_path, fault):
    store, scope, run_id = _reserved_store(tmp_path)
    home, evidence = recovery_files(tmp_path)
    if fault.startswith("native_") or fault == "baseline_session_id":
        evidence.pop(fault)
    elif fault == "unsupported-version":
        evidence["version"] = 99
    elif fault == "boolean-version":
        evidence["version"] = True
    elif fault == "failure-shape":
        evidence["native_usage_failure"] = []
    elif fault == "invalid-card":
        evidence["native_rate_table"] = {}
    elif fault == "unfinished-child":
        write(home, CHILD, child_records(tmp_path, complete=False))
    elif fault == "huge-start":
        evidence["native_usage_started_at"] = 10**1000
    elif fault == "wrong-root-model":
        evidence["native_root_sessions"][ROOT] = "luna"
    elif fault == "currency-conflict":
        evidence["native_rate_table"]["currency"] = "EUR"
    else:
        evidence["native_usage_failure"] = "previous observation failed"
    directory = tmp_path / "evidence"
    write_spend_evidence(str(directory), str(run_id), evidence)
    control_reason = "legacy completed return awaiting reconciliation"
    store.record_reservation_retained(run_id, control_reason)
    _end_run_at(store, run_id, ENDED)
    assert CodexSpendReconciler(store, (str(directory),)).reconcile(run_id)
    assert store.active_reservation_runs() == ()
    expected = 1224 if fault in ("unfinished-child", "sticky-failure") else 0
    assert store.snapshot(scope).settled == Money("USD", expected)



def test_recovery_subtracts_prior_leg_baseline_only_from_its_original_root(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path)
    home, evidence = recovery_files(tmp_path)
    baseline = {"input_tokens": 1000, "output_tokens": 10, "cache_read_input_tokens": 500, "source": "rollout"}
    doubled = {"input_tokens": 2000, "cached_input_tokens": 1000, "output_tokens": 20}
    write(home, ROOT, [metadata(ROOT, tmp_path), event("event_msg", {"type": "token_count", "info": {"total_token_usage": doubled}})])
    fresh = {"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 10}
    write(home, OTHER, [metadata(OTHER, tmp_path), *spawn(OTHER),
                       event("event_msg", {"type": "token_count", "info": {"total_token_usage": fresh}})])
    write(home, CHILD, child_records(tmp_path, parent=OTHER))
    evidence.update(session_id=OTHER, baseline_session_id=ROOT, baseline_usage=baseline,
                    native_root_sessions={ROOT: "sol", OTHER: "sol"})
    directory = tmp_path / "evidence"
    write_spend_evidence(str(directory), str(run_id), evidence)
    _end_run_at(store, run_id, ENDED)
    assert CodexSpendReconciler(store, (str(directory),)).reconcile(run_id)
    assert store.snapshot(scope).settled == Money("USD", 2374)


def test_root_only_recovery_uses_frozen_cache_write_price(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path)
    home, evidence = recovery_files(tmp_path)
    (home / "sessions" / NOW.strftime("%Y/%m/%d") / ("rollout-now-" + CHILD + ".jsonl")).unlink()
    write(home, ROOT, [metadata(ROOT, tmp_path), event("event_msg", {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 1000, "cache_write_input_tokens": 800, "output_tokens": 10}}})])
    directory = tmp_path / "evidence"
    write_spend_evidence(str(directory), str(run_id), evidence)
    _end_run_at(store, run_id, ENDED)
    assert CodexSpendReconciler(store, (str(directory),)).reconcile(run_id)
    assert store.snapshot(scope).settled == Money("USD", 2500)


def test_recovery_price_exceeding_sqlite_bound_settles_zero_estimate(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path)
    home, evidence = recovery_files(tmp_path)
    evidence["native_rate_table"]["rates"]["sol"]["input_micros"] = 10**18
    write(home, ROOT, [metadata(ROOT, tmp_path), event("event_msg", {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 10**12, "output_tokens": 0}}})])
    directory = tmp_path / "evidence"
    write_spend_evidence(str(directory), str(run_id), evidence)
    _end_run_at(store, run_id, ENDED)
    assert CodexSpendReconciler(store, (str(directory),)).reconcile(run_id)
    assert store.snapshot(scope).settled == Money.zero()
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")


def native_executable(tmp_path, *, mode="chatty", complete=True):
    root = [metadata(ROOT, tmp_path), *spawn(), event("event_msg", {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 10}}})]
    script = tmp_path / "fake-codex"
    script.write_text(f"#!{sys.executable}\n" + f'''
import json, os, pathlib, subprocess, sys, time
from datetime import datetime, timezone
root, child = {root!r}, {child_records(tmp_path, complete=complete)!r}
home = pathlib.Path(os.environ["CODEX_HOME"])
counter = pathlib.Path({str(tmp_path / "attempts")!r})
attempt = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(attempt))
now = datetime.now(timezone.utc)
day = home / "sessions" / now.strftime("%Y/%m/%d")
day.mkdir(parents=True, exist_ok=True)
sid = {ROOT!r}
append = False
records_by_session = [(sid, root), ({CHILD!r}, child)]
if {mode!r}.startswith("retry") and attempt > 1:
    sid = {OTHER!r} if {mode!r} == "retry-fresh" else {ROOT!r}
    root = [root[0], root[-1]]
    root[0]["payload"].update(id=sid, session_id=sid)
    append = sid == {ROOT!r}
    if append:
        root = root[-1:]
        root[0]["payload"]["info"]["total_token_usage"] = {{"input_tokens": 2000, "cached_input_tokens": 1000, "output_tokens": 20}}
    records_by_session = [(sid, root)]
for session, records in records_by_session:
    for i, record in enumerate(records):
        record.update(timestamp=now.isoformat(), ordinal=i)
    with (day / ("rollout-now-" + session + ".jsonl")).open("a" if append else "w") as stream:
        stream.write("".join(json.dumps(r) + "\\n" for r in records))
if {mode!r} == "discovery-failure":
    for i in range(513):
        (day / ("rollout-unrelated-" + str(i) + "-" + {ROOT!r} + ".jsonl")).write_text(json.dumps(root[0]) + "\\n")
if {mode!r} == "invalid-child":
    child[0]["payload"]["cli_version"] = "0.160.1"
    (day / ("rollout-now-" + {CHILD!r} + ".jsonl")).write_text("".join(json.dumps(r) + "\\n" for r in child))
if {mode!r} == "unpriced-child":
    child = [r for r in child if r["type"] not in {{"turn_context", "token_usage_record"}}]
    (day / ("rollout-now-" + {CHILD!r} + ".jsonl")).write_text("".join(json.dumps(r) + "\\n" for r in child))
worker = subprocess.Popen([sys.executable, "-S", "-c", "import time; time.sleep(60)"])
pathlib.Path({str(tmp_path / "ready.json")!r}).write_text(json.dumps({{"pid": os.getpid(), "worker": worker.pid}}))
print("session id: " + sid, file=sys.stderr, flush=True)
if {mode!r}.startswith("retry") and attempt == 1:
    worker.terminate()
    worker.wait()
    print("stream disconnected before completion", file=sys.stderr, flush=True)
    sys.exit(1)
if {mode!r} in {{"clean", "pre-final"}} or {mode!r}.startswith("retry"):
    worker.terminate()
    worker.wait()
    if {mode!r} == "pre-final":
        import signal
        print("before final", file=sys.stderr, flush=True)
        signal.pause()
    pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps({{"status": "pushed", "summary": "done", "needs": ""}}))
else:
    for i in range(100):
        print("working", file=sys.stderr, flush=True)
        time.sleep(.05)
pathlib.Path({str(tmp_path / "finished")!r}).write_text("finished")
''')
    script.chmod(0o700)
    return script


def native_runner(tmp_path, script, directory):
    return CodexRunner(
        codex_bin=str(script), default_model="sol", rate_table=RATES,
        env={"OPENAI_API_KEY": "test-key"}, poll_slice=.02,
        cost_per_mtok_in=2, cost_per_mtok_out=10, cost_per_mtok_cached_in=(("sol", .1),),
        backoff_base=0, spend_evidence_dir=str(directory),
        process_supervisor=ProcessSupervisor(str(tmp_path / "supervision"), grace_seconds=.05),
    )


@pytest.mark.parametrize("mode", ["discovery-failure", "invalid-child", "unpriced-child"])
def test_observer_limits_fail_closed(tmp_path, mode):
    directory = tmp_path / "evidence"
    logs = []
    runner = replace(native_runner(tmp_path, native_executable(tmp_path, mode=mode), directory), log=logs.append)
    wrapper, control = _wrapper(tmp_path, runner, evidence_dir=str(directory))
    wrapper = replace(wrapper, rate_table=RATES,
                      budget_policy=BudgetPolicy({("fix", "RESOLVE"): Money("USD", 2000)}))
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    result = asyncio.run(wrapper.run(RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=.002,
                                                env={"CODEX_HOME": str(tmp_path / "codex")})))
    assert not (tmp_path / "finished").exists()
    assert result.exit_reason is ExitReason.ACCOUNTING_UNAVAILABLE
    assert any("ACCOUNTING" in line and "killing subprocess" in line for line in logs)
    assert isinstance(result.run_usage, EstimatedRunUsage)
    assert control.active_reservation_runs() == ()
    with control.transaction() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 1


@pytest.mark.parametrize("mode", ["chatty", "clean", "pre-final"])
@pytest.mark.parametrize("pricing", ["card", "card-only", "scalar"])
def test_native_child_crossing_cap_stops_inflight_or_final_success_under_one_reservation(
    tmp_path, mode, pricing, monkeypatch,
):
    spawn_process = asyncio.create_subprocess_exec

    async def spawn_at_boundary(*args, **kwargs):
        proc = await spawn_process(*args, **kwargs)
        # Usage is already over cap before the fake writes its final output.
        # Select each ordering explicitly instead of racing the watchdog poll.
        assert proc.stderr is not None
        if mode == "clean":
            await proc.stderr.read()
        elif mode == "pre-final":
            await proc.stderr.readuntil(b"before final\n")
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_at_boundary)
    directory = tmp_path / "evidence"
    runner = native_runner(tmp_path, native_executable(tmp_path, mode=mode), directory)
    if mode == "clean":
        # Final accounting must enforce the cap without a periodic sample.
        runner = replace(runner, wall_clock=lambda: 0)
    if pricing == "scalar":
        runner = replace(runner, rate_table=None,
                         cost_per_mtok=(("sol", 2., 10.), ("luna", .1, .5)),
                         cost_per_mtok_cached_in=(("sol", .1), ("luna", .01)))
    elif pricing == "card-only":
        runner = replace(runner, cost_per_mtok_in=0, cost_per_mtok_out=0, cost_per_mtok_cached_in=(("sol", 0),))
    wrapper, control = _wrapper(tmp_path, runner, evidence_dir=str(directory))
    wrapper = replace(wrapper, rate_table=RATES,
                      budget_policy=BudgetPolicy({("fix", "RESOLVE"): Money("USD", 1200)}))
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    async def run():
        # Bound failure cleanup if the watchdog stops enforcing the cap;
        # the process/stream events above determine the race ordering.
        try:
            return await asyncio.wait_for(wrapper.run(RuntimePlan(
                "work", cwd=str(tmp_path), model="sol", max_budget_usd=.0012,
                env={"CODEX_HOME": str(tmp_path / "codex")},
            )), timeout=30)
        except BaseException:
            runner.process_supervisor.force_kill_all()
            raise

    result = asyncio.run(run())
    assert (tmp_path / "finished").exists() == (mode == "clean")
    if mode == "clean":
        assert result.summary == "done"
    else:
        assert "mid-run; killed" in result.summary
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert result.cost_usd == pytest.approx(.001224)
    assert isinstance(result.run_usage, EstimatedRunUsage)
    assert control.active_reservation_runs() == ()
    with control.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT amount_micros FROM charges").fetchone()[0] == 1224


def test_cancellation_kills_owned_groups_and_keeps_incomplete_child_evidence_for_recovery(tmp_path):
    directory = tmp_path / "evidence"
    runner = native_runner(tmp_path, native_executable(tmp_path, complete=False), directory)
    wrapper, control = _wrapper(tmp_path, runner, evidence_dir=str(directory))
    wrapper = replace(wrapper, rate_table=RATES)
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    identities = []

    async def scenario():
        task = asyncio.create_task(wrapper.run(RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=2,
                                                         env={"CODEX_HOME": str(tmp_path / "codex")})))
        deadline = time.monotonic() + 5
        try:
            while not (tmp_path / "ready.json").exists():
                assert time.monotonic() < deadline
                await asyncio.sleep(.01)
            for pid in json.loads((tmp_path / "ready.json").read_text()).values():
                identity = ProcessSupervisor.process_identity(pid)
                assert identity is not None
                identities.append(identity)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert all(not ProcessSupervisor.matches_identity(i.pid, i.start_ticks) for i in identities)
    # Operator policy (2026-10-11): the interrupted request costs zero. Receipts are only priced
    # inside the run's recorded end, so the reservation waits for it and then settles at 1224.
    run_id = control.active_reservation_runs()[0]
    _end_run_at(control, run_id, ENDED)
    assert CodexSpendReconciler(control, (str(directory),)).reconcile(run_id)
    assert control.active_reservation_runs() == ()
    with control.transaction() as db:
        assert db.execute("SELECT amount_micros FROM charges").fetchall()[0][0] == 1224


@pytest.mark.parametrize("resume", [True, False])
def test_transport_retry_prices_every_attempt_and_child_without_another_admission(tmp_path, resume):
    directory = tmp_path / "evidence"
    runner = native_runner(tmp_path, native_executable(tmp_path, mode="retry-resume" if resume else "retry-fresh"), directory)
    runner = replace(runner, resume_on_retry=resume)
    wrapper, control = _wrapper(tmp_path, runner, evidence_dir=str(directory))
    wrapper = replace(wrapper, rate_table=RATES)
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    result = asyncio.run(wrapper.run(RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=2,
                                                env={"CODEX_HOME": str(tmp_path / "codex")})))
    assert (tmp_path / "attempts").read_text() == "2", (result.summary, result.exit_reason)
    assert result.status == "pushed"
    assert result.cost_usd == pytest.approx(.002374)
    assert control.active_reservation_runs() == ()
    with control.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 1
        assert db.execute("SELECT amount_micros FROM charges").fetchone()[0] == 2374


def test_clean_exit_uses_captured_card_even_when_legacy_scalar_would_cross_cap(tmp_path):
    directory = tmp_path / "evidence"
    runner = native_runner(tmp_path, native_executable(tmp_path, mode="clean"), directory)
    runner = replace(runner, cost_per_mtok_in=20)
    result = asyncio.run(runner.run(RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=.01,
                                               env={"CODEX_HOME": str(tmp_path / "codex")})))
    assert result.status == "pushed"
    assert result.exit_reason is ExitReason.OK
    assert result.cost_usd == pytest.approx(.001224)


def test_malformed_optional_baseline_preserves_in_window_root_and_child_evidence(tmp_path):
    store, scope, run_id = _reserved_store(tmp_path, runtime="codex")
    home, evidence = recovery_files(tmp_path)
    root_tokens = {"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 10}
    write(home, ROOT, [metadata(ROOT, tmp_path), *spawn(),
        event("turn_context", {"turn_id": "t", "model": "sol"}),
        event("token_usage_record", {"thread_id": ROOT, "turn_id": "t", "response_id": "root",
                                     "usage": root_tokens}),
        event("event_msg", {"type": "token_count", "info": {
            "last_token_usage": root_tokens, "total_token_usage": root_tokens}})])
    evidence.update(baseline_usage={"input_tokens": "malformed"}, baseline_session_id=ROOT)
    directory = tmp_path / "evidence"
    write_spend_evidence(str(directory), str(run_id), evidence)
    _end_run_at(store, run_id, ENDED)
    assert CodexSpendReconciler(store, (str(directory),)).reconcile(run_id)
    assert store.active_reservation_runs() == ()
    assert store.snapshot(scope).settled == Money("USD", 1224)

    with store.transaction() as db:
        assert db.execute("SELECT basis FROM charges").fetchone()[0] == "estimate"


@pytest.mark.parametrize("reported_micros", [None, 900])
def test_v2_recovery_without_native_rates_keeps_frozen_scalar_request_pricing(tmp_path, reported_micros):
    from tests.adapters.test_codex_child_usage import path
    from tests.adapters.test_settlement_regressions import receipt, counter

    store, scope, run_id = _reserved_store(tmp_path, runtime="codex")
    home, evidence = recovery_files(tmp_path)
    path(home, CHILD).unlink()
    records = [metadata(ROOT, tmp_path), event("turn_context", {"turn_id": "paid", "model": "sol"})]
    if reported_micros is not None:
        paid = receipt(ROOT, "paid", 50)
        paid["payload"]["cost_usd"] = reported_micros / 1_000_000
        records.append(paid)
    write(home, ROOT, [*records, counter(100)])
    evidence["native_rate_table"] = None
    directory = tmp_path / "evidence"
    write_spend_evidence(str(directory), str(run_id), evidence)
    _end_run_at(store, run_id, ENDED)

    reconciler = CodexSpendReconciler(store, (str(directory),))
    assert reconciler.reconcile(run_id)
    assert store.snapshot(scope).settled == Money("USD", 200 if reported_micros is None else 1000)
    reconciler.reconcile(run_id)
    assert store.snapshot(scope).settled == Money("USD", 200 if reported_micros is None else 1000)
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 1
    assert not Path(spend_evidence_path(str(directory), str(run_id))).exists()
    assert store.active_reservation_runs() == ()


@pytest.mark.parametrize("usage_kind,expected_basis", [
    ("estimated", "estimate"), ("measured", "configured_rate_card"),
    ("reported", "provider_reported"), ("streamed", "estimate"),
])
def test_journalled_run_cost_matches_the_committed_settlement(tmp_path, usage_kind, expected_basis):
    from issue_agent.adapters.journal import Journal, JournalingRuntime

    sample = ModelUsage(ROOT, "sol", TokenUsage(100, 0, source=UsageSource.ROLLOUT),
                        reported_cost=Money("USD", 200) if usage_kind == "reported" else None)
    observed = (EstimatedRunUsage("one request has no receipt", (sample,), RATES)
                if usage_kind == "estimated" else MeasuredRunUsage((sample,), RATES))
    result = AgentResult(False, status="pushed", session_id=ROOT,
                         run_usage=None if usage_kind == "streamed" else observed,
                         usage={"input_tokens": 100, "output_tokens": 0, "source": "rollout"})
    wrapper, store = _wrapper(tmp_path, Runtime(result))
    wrapper = replace(wrapper, rate_table=RATES)
    journaled = JournalingRuntime(wrapper, Journal(str(tmp_path / "journal.jsonl")),
                                  repo="o/r", role="fix", runtime="codex")
    journaled.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)

    asyncio.run(journaled.run(RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=2)))
    completed = next(r for r in (json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines())
                     if r["event"] == "agent_run" and "cost_usd" in r)
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (200, expected_basis)
    assert completed["cost_usd"] == 0.0002
