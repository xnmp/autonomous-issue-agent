"""Codex money end to end: one admission, root-rollout pricing, settle once,
interrupted requests at zero, and recovery of a cancelled run."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import sys
import time

import pytest

from issue_agent.adapters.codex_runner import CodexRunner
from issue_agent.adapters.journal import Journal, JournalingRuntime
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.adapters.spend_recovery import CodexSpendReconciler
from issue_agent.domain.contracts import Money, TokenUsage, UsageSource
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.pricing import ModelRates, RateTable
from issue_agent.domain.run_usage import EstimatedRunUsage, MeasuredRunUsage, ModelUsage
from tests.adapters.test_accounting_runtime import Runtime, _db, _lease, _run, _wrapper
from tests.adapters.test_spend_recovery import _end_run_at

ROOT = "019fa244-0000-7de0-8e18-e56f423bbbcc"
RATES = RateTable("card", "v1", "USD", {"sol": ModelRates(2_000_000, 10_000_000, 100_000, 2_500_000)})
# 500 uncached in x $2 + 10 out x $10 + 500 cached x $0.10 per MTok.
ROOT_MICROS = 1_000 + 100 + 50
ENDED = datetime(2100, 1, 1, tzinfo=timezone.utc)


def test_interrupted_unusaged_request_settles_zero_without_the_reserved_maximum(tmp_path):
    result = AgentResult(True, exit_reason=ExitReason.NO_RESULT, run_usage=EstimatedRunUsage("interrupted request"))
    wrapper, control = _wrapper(tmp_path, Runtime(result))
    wrapper.set_context("fix", "RESOLVE", 1, 1)
    _run(wrapper, RuntimePlan("work", model="sol", max_budget_usd=2))
    assert control.active_reservation_runs("o/r:fix") == ()
    with _db(control) as db:
        assert tuple(db.execute("SELECT charged_micros,basis FROM legs WHERE state='ended'").fetchone()) == (0, "estimate")


@pytest.mark.parametrize("usage_kind,expected_basis", [
    ("estimated", "estimate"), ("measured", "configured_rate_card"),
    ("reported", "provider_reported"), ("streamed", "estimate"),
])
def test_journalled_run_cost_matches_the_committed_settlement(tmp_path, usage_kind, expected_basis):
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
    journaled.set_context("fix", "RESOLVE", 1, 1)
    _lease(wrapper)

    asyncio.run(journaled.run(RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=2)))
    completed = next(r for r in (json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines())
                     if r["event"] == "agent_run" and "cost_usd" in r)
    with _db(store) as db:
        assert tuple(db.execute("SELECT charged_micros,basis FROM legs WHERE state='ended'").fetchone()) == (200, expected_basis)
    assert completed["cost_usd"] == 0.0002


def _fake_codex(tmp_path, mode):
    """A codex that writes its root rollout, then behaves per `mode`."""
    script = tmp_path / "fake-codex"
    script.write_text(f"#!{sys.executable}\n" + f'''
import json, os, pathlib, subprocess, sys, time
from datetime import datetime, timezone
counter = pathlib.Path({str(tmp_path / "attempts")!r})
attempt = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(attempt))
now = datetime.now(timezone.utc).isoformat()
day = pathlib.Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "10" / "13"
day.mkdir(parents=True, exist_ok=True)
total = {{"input_tokens": 1000 * attempt, "cached_input_tokens": 500 * attempt, "output_tokens": 10 * attempt}}
records = [{{"timestamp": now, "type": "session_meta", "payload": {{"id": {ROOT!r}, "originator": "codex_exec",
            "source": "exec", "cwd": os.getcwd()}}}}] if attempt == 1 else []
records.append({{"timestamp": now, "type": "event_msg", "payload": {{"type": "token_count",
                "info": {{"total_token_usage": total}}}}}})
with (day / "rollout-now-{ROOT}.jsonl").open("a") as stream:
    stream.write("".join(json.dumps(r) + "\\n" for r in records))
worker = subprocess.Popen([sys.executable, "-S", "-c", "import time; time.sleep(60)"])
pathlib.Path({str(tmp_path / "ready.json")!r}).write_text(json.dumps({{"pid": os.getpid(), "worker": worker.pid}}))
if {mode!r} == "retry" and attempt == 1:
    worker.terminate(); worker.wait()
    print("stream disconnected before completion", file=sys.stderr, flush=True)
    sys.exit(1)
if {mode!r} == "hang":
    for i in range(200):
        print("working", file=sys.stderr, flush=True)
        time.sleep(.05)
worker.terminate(); worker.wait()
pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps({{"status": "pushed", "summary": "done"}}))
''')
    script.chmod(0o700)
    return script


def _runner(tmp_path, mode, directory):
    return CodexRunner(
        codex_bin=str(_fake_codex(tmp_path, mode)), default_model="sol", rate_table=RATES,
        env={"OPENAI_API_KEY": "test-key"}, poll_slice=.02,
        spend_evidence_dir=str(directory),
        process_supervisor=ProcessSupervisor(str(tmp_path / "supervision"), grace_seconds=.05),
    )


def _plan(tmp_path):
    return RuntimePlan("work", cwd=str(tmp_path), model="sol", max_budget_usd=2,
                       env={"CODEX_HOME": str(tmp_path / "codex")})


def test_a_dropped_launch_is_one_leg_settled_at_its_rollout_and_its_evidence_removed(tmp_path):
    directory = tmp_path / "evidence"
    wrapper, control = _wrapper(tmp_path, _runner(tmp_path, "retry", directory), spend_evidence_dir=str(directory))
    wrapper = replace(wrapper, rate_table=RATES)
    wrapper.set_context("fix", "RESOLVE", 1, 1)

    result = _run(wrapper, _plan(tmp_path))

    assert (tmp_path / "attempts").read_text() == "1"
    assert result.exit_reason is ExitReason.TRANSPORT_DEATH
    assert control.active_reservation_runs("o/r:fix") == ()
    with _db(control) as db:
        assert db.execute("SELECT count(*) FROM legs").fetchone()[0] == 1
        assert db.execute("SELECT charged_micros FROM legs WHERE state='ended'").fetchone()[0] == ROOT_MICROS
    assert not any(directory.iterdir())


def test_cancellation_kills_owned_groups_and_recovery_settles_the_rollout(tmp_path):
    directory = tmp_path / "evidence"
    wrapper, control = _wrapper(tmp_path, _runner(tmp_path, "hang", directory), spend_evidence_dir=str(directory))
    wrapper = replace(wrapper, rate_table=RATES)
    wrapper.set_context("fix", "RESOLVE", 1, 1)
    identities = []
    _lease(wrapper)

    async def scenario():
        task = asyncio.create_task(wrapper.run(_plan(tmp_path)))
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
    run_id = control.active_reservation_runs("o/r:fix")[0]
    _end_run_at(control, run_id, ENDED)
    assert CodexSpendReconciler(control, (str(directory),)).reconcile(run_id)
    assert control.active_reservation_runs("o/r:fix") == ()
    with _db(control) as db:
        assert db.execute("SELECT charged_micros FROM legs WHERE state='ended'").fetchone()[0] == ROOT_MICROS
