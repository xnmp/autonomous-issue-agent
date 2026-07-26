"""An aborted agent run must still be costed.

Cost only ever arrives on the SDK's final ResultMessage, so a run killed by the
wall cap / idle watchdog / a dropped result reported cost_usd=0.0 AND usage=None.
Observed on the first real deployment (xnmp/tauri-explorer, 2026-07-26): a resolve
ran 906s, made four commits, hit AGENT_MAX_RUNTIME_SECONDS and ledgered $0.00. The
spend caps sum the ledger, so repeated timeouts accumulate real money that neither
the issue cap nor the daily cap can see — the cap is evadable by timing out.

Two halves, tested here and in test_agent_runner.py::merge_usage:
  * the runner keeps a running token total, so an abort knows what it spent;
  * the journal prices those tokens at a rate LEARNED from completed runs, so no
    price table can go stale, and records it flagged as inferred.

Assertions are on the ledger the caps actually read, never on how the number is
derived.
"""
from __future__ import annotations

import asyncio

from issue_agent.adapters.journal import (
    Journal,
    JournalingRuntime,
    estimate_cost_usd,
    observed_cost_per_token,
    read_events,
    total_tokens,
)
from issue_agent.domain.agent_task import AgentTask
from issue_agent.domain.outcome import AgentResult, ExitReason

ABORT_USAGE = {
    "input_tokens": 84,
    "output_tokens": 22349,
    "cache_read_input_tokens": 2671473,
    "cache_creation_input_tokens": 72415,
}


class FakeInner:
    def __init__(self, result):
        self.result = result

    async def run(self, task):
        return self.result


def _runtime(tmp_path, result, repo="o/r"):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    ledger = str(tmp_path / "spend.jsonl")
    return JournalingRuntime(FakeInner(result), journal, ledger, repo, "fix"), journal, ledger


def _spend(ledger_path) -> list[dict]:
    return [e for e in read_events(ledger_path) if e.get("event") == "spend"]


def _seed_priced_history(rt, journal, cost=3.826, usage=None, stream_usage=None):
    """One COMPLETED run, so the rate has something to learn from."""
    rt.set_context("fix", "RESOLVE", 1, 1)
    rt.inner.result = AgentResult(
        is_error=False, status="pr_opened", cost_usd=cost,
        exit_reason=ExitReason.OK, usage=usage or dict(ABORT_USAGE),
        stream_usage=stream_usage,
    )
    asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))


# ── the pure rate/price helpers ───────────────────────────────────────────────
def test_total_tokens_sums_every_counter_and_ignores_non_numbers():
    assert total_tokens(ABORT_USAGE) == 2766321
    assert total_tokens({"input_tokens": 5, "note": "x", "flag": True}) == 5
    assert total_tokens(None) == 0 and total_tokens("nope") == 0


def test_rate_is_learned_from_runs_that_reported_both_cost_and_tokens():
    events = [
        {"event": "agent_run", "cost_usd": 2.0, "usage": {"input_tokens": 1_000_000}},
        {"event": "agent_run", "cost_usd": 4.0, "usage": {"input_tokens": 1_000_000}},
    ]
    assert observed_cost_per_token(events) == 3e-6  # 6.0 / 2_000_000, blended


def test_rate_is_zero_without_evidence_so_nothing_is_invented():
    assert observed_cost_per_token([]) == 0.0
    # cost but no usage, and usage but no cost: neither can price anything
    assert observed_cost_per_token([
        {"event": "agent_run", "cost_usd": 5.0, "usage": None},
        {"event": "agent_run", "cost_usd": 0.0, "usage": {"input_tokens": 999}},
    ]) == 0.0


def test_rate_is_learned_in_stream_units_when_the_run_carries_both_totals():
    """A completed run's stream-summed total runs ~3.5x its ResultMessage total
    (measured 43.2M vs 12.1M, 2026-07-26). Aborted runs only ever have the
    stream total, so the rate's denominator must be the stream total too —
    learning against the RESULT total priced every abort ~3.5x high."""
    events = [{
        "event": "agent_run", "cost_usd": 12.0,
        "usage": {"input_tokens": 12_000_000},
        "usage_stream": {"input_tokens": 42_000_000},
    }]
    assert observed_cost_per_token(events) == 12.0 / 42_000_000


def test_stream_denominated_evidence_outranks_legacy_result_denominated_evidence():
    """Once one calibrated pair exists, pre-fix journal lines (usage only, in
    result units) must stop polluting the rate — mixing the two units in one
    blend re-inflates every estimate."""
    legacy = {"event": "agent_run", "cost_usd": 12.0,
              "usage": {"input_tokens": 12_000_000}}
    paired = {"event": "agent_run", "cost_usd": 6.0,
              "usage": {"input_tokens": 6_000_000},
              "usage_stream": {"input_tokens": 21_000_000}}
    assert observed_cost_per_token([legacy, paired]) == 6.0 / 21_000_000


def test_an_estimate_never_becomes_evidence_for_the_next_estimate():
    """Estimated figures live in cost_estimated_usd, so feeding a journal full of
    them back in yields NO rate — otherwise the rate would drift on its own
    output."""
    events = [
        {"event": "agent_run", "cost_usd": 0.0, "cost_estimated_usd": 3.83,
         "usage": dict(ABORT_USAGE)}
        for _ in range(5)
    ]
    assert observed_cost_per_token(events) == 0.0


def test_estimate_refuses_to_guess_without_a_rate_or_tokens():
    assert estimate_cost_usd(ABORT_USAGE, 0.0) == 0.0
    assert estimate_cost_usd(None, 1e-6) == 0.0
    assert estimate_cost_usd({}, 1e-6) == 0.0


# ── the ledger the spend caps actually read ──────────────────────────────────
def test_a_wall_capped_run_reaches_the_ledger_instead_of_vanishing(tmp_path):
    """The regression. Before: this run wrote no spend line at all."""
    rt, journal, ledger = _runtime(tmp_path, AgentResult(is_error=False))
    _seed_priced_history(rt, journal)

    rt.inner.result = AgentResult(
        is_error=True, status="timeout", cost_usd=0.0,
        exit_reason=ExitReason.WALL_TIMEOUT, usage=dict(ABORT_USAGE),
    )
    rt.set_context("fix", "RESOLVE", 2, 2)
    asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))

    aborted = [s for s in _spend(ledger) if s["issue"] == 2]
    assert len(aborted) == 1
    assert aborted[0]["cost_usd"] > 0
    assert aborted[0]["estimated"] is True


def test_the_estimate_matches_what_the_same_tokens_really_cost(tmp_path):
    """Priced off a completed run with identical usage, so the inferred figure
    must land on that run's actual cost."""
    rt, journal, ledger = _runtime(tmp_path, AgentResult(is_error=False))
    _seed_priced_history(rt, journal, cost=3.826)

    rt.inner.result = AgentResult(
        is_error=True, status="timeout", cost_usd=0.0,
        exit_reason=ExitReason.WALL_TIMEOUT, usage=dict(ABORT_USAGE),
    )
    rt.set_context("fix", "RESOLVE", 2, 2)
    asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))

    est = [s for s in _spend(ledger) if s["issue"] == 2][0]["cost_usd"]
    assert abs(est - 3.826) < 0.01


def test_a_skewed_abort_is_priced_near_truth_not_3_5x_high(tmp_path):
    """The 2026-07-26 units bug, end to end. A completed run journals BOTH totals
    (result 2.77M / stream 9.68M — the observed ~3.5x skew) at $3.826; a wall
    abort then spends the same stream-magnitude tokens. The ledger figure must
    land on ~$3.83, not ~$13.4 (which is what rate-in-result-units × stream
    tokens produced, silently eating the issue/daily caps)."""
    stream = {k: v * 35 // 10 for k, v in ABORT_USAGE.items()}
    rt, journal, ledger = _runtime(tmp_path, AgentResult(is_error=False))
    _seed_priced_history(rt, journal, cost=3.826, stream_usage=dict(stream))

    rt.inner.result = AgentResult(
        is_error=True, status="timeout", cost_usd=0.0,
        exit_reason=ExitReason.WALL_TIMEOUT,
        usage=dict(stream), stream_usage=dict(stream),
    )
    rt.set_context("fix", "RESOLVE", 2, 2)
    asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))

    est = [s for s in _spend(ledger) if s["issue"] == 2][0]["cost_usd"]
    assert abs(est - 3.826) < 0.01


def test_a_real_cost_is_recorded_as_measured_not_estimated(tmp_path):
    rt, journal, ledger = _runtime(tmp_path, AgentResult(
        is_error=False, status="pr_opened", cost_usd=1.25,
        exit_reason=ExitReason.OK, usage=dict(ABORT_USAGE),
    ))
    rt.set_context("fix", "RESOLVE", 7, 7)
    asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))

    line = _spend(ledger)[0]
    assert line["cost_usd"] == 1.25
    assert line.get("estimated") is None  # measured, not inferred
    run = [e for e in read_events(journal.path) if e["event"] == "agent_run"][0]
    assert run["cost_usd"] == 1.25 and run.get("cost_estimated_usd") is None


def test_an_abort_with_no_history_records_no_fabricated_cost(tmp_path):
    """A fresh install cannot price anything, and must not pretend otherwise."""
    rt, journal, ledger = _runtime(tmp_path, AgentResult(
        is_error=True, status="timeout", cost_usd=0.0,
        exit_reason=ExitReason.WALL_TIMEOUT, usage=dict(ABORT_USAGE),
    ))
    rt.set_context("fix", "RESOLVE", 3, 3)
    asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))

    assert _spend(ledger) == []
    run = [e for e in read_events(journal.path) if e["event"] == "agent_run"][0]
    assert run["cost_usd"] == 0.0
    assert run["usage"]["output_tokens"] == 22349  # tokens still recorded


def test_an_abort_that_reported_no_tokens_stays_out_of_the_ledger(tmp_path):
    """A run that died before its first turn spent nothing; a $0.00 line would be
    noise in the file both caps scan on every dispatch."""
    rt, journal, ledger = _runtime(tmp_path, AgentResult(is_error=False))
    _seed_priced_history(rt, journal)

    rt.inner.result = AgentResult(
        is_error=True, status="timeout", cost_usd=0.0,
        exit_reason=ExitReason.WALL_TIMEOUT, usage=None,
    )
    rt.set_context("fix", "RESOLVE", 4, 4)
    asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))

    assert [s for s in _spend(ledger) if s["issue"] == 4] == []


def test_estimated_spend_counts_toward_the_caps_that_scan_the_ledger(tmp_path):
    """The point of the fix: `sum_issue`/`sum_today` must SEE an aborted run, or
    the cap is evadable by timing out repeatedly."""
    from issue_agent.adapters.journal import sum_issue

    rt, journal, ledger = _runtime(tmp_path, AgentResult(is_error=False))
    _seed_priced_history(rt, journal)

    rt.inner.result = AgentResult(
        is_error=True, status="timeout", cost_usd=0.0,
        exit_reason=ExitReason.WALL_TIMEOUT, usage=dict(ABORT_USAGE),
    )
    for _ in range(3):
        rt.set_context("fix", "RESOLVE", 9, 9)
        asyncio.run(rt.run(AgentTask(prompt="p", model="opus")))

    # three timed-out resolves on issue 9, each ~$3.83
    assert sum_issue(ledger, "o/r", 9) > 10.0
