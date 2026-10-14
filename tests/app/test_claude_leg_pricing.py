"""Claude legs settle on tokens x the harness rate table, never the CLI's dollars (#389).

Envelope shapes are the ones CLI 2.1.294 (claude-agent-sdk 0.2.165) emitted on
2026-10-14: `modelUsage` names every model a leg used, subagents included, and
is session-cumulative on resume, as is `total_cost_usd`; the top-level `usage`
is the leg's own main loop only.
"""

import asyncio
import sqlite3
import sys
import types
from dataclasses import dataclass

import pytest

from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.agent_runner import AgentRunner
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.app.config import from_env, rate_table
from issue_agent.domain.contracts import Money
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.pricing import BudgetPolicy, UnknownModelPolicy
from issue_agent.domain.run_usage import EstimatedRunUsage, MeasuredRunUsage, parse_snapshot, snapshot_record

CLAUDE = rate_table(from_env({}), "claude")
SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"


def _model(inputs, outputs, reads, writes, **extra):
    return {"inputTokens": inputs, "outputTokens": outputs, "cacheReadInputTokens": reads,
            "cacheCreationInputTokens": writes, "costUSD": 0.0, "webSearchRequests": 0, **extra}


def _usage(inputs, outputs, reads, writes):
    return {"input_tokens": inputs, "output_tokens": outputs, "cache_read_input_tokens": reads,
            "cache_creation_input_tokens": writes,
            "cache_creation": {"ephemeral_1h_input_tokens": writes, "ephemeral_5m_input_tokens": 0}}


@dataclass
class _ResultMessage:
    model_usage: object = None
    usage: object = None
    total_cost_usd: float = 0.0
    session_id: str = "s1"
    subtype: str = "success"
    is_error: bool = False
    structured_output: object = None
    result: str = ""


def _leg(monkeypatch, result, plan=None, before=(), rates=CLAUDE):
    """One Claude leg through AgentRunner over a stand-in SDK stream; returns
    (AgentResult, daemon log lines)."""
    async def query(prompt, options):
        for message in (*before, result):
            yield message

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = lambda **_kw: object()
    fake.ResultMessage = _ResultMessage
    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    lines: list[str] = []
    runner = AgentRunner(log=lines.append, rate_table=rates)
    out = asyncio.run(runner.run(plan or RuntimePlan(prompt="x", model=SONNET, max_budget_usd=10)))
    return out, lines


def _subagent_started():
    return types.SimpleNamespace(subtype="task_started", data={"task_id": "t1", "task_type": "local_agent"})


# The fresh two-model leg CLI 2.1.294 reported: a Sonnet main loop and a Haiku subagent.
_FRESH = {SONNET: _model(6, 203, 50_407, 9_474), HAIKU: _model(4, 370, 13_028, 14_639)}
# Sonnet $2/$10, reads $0.10, 1h writes $4 per MTok: 12 + 2030 + 5041 + 37896 micros.
_SONNET_MICROS = 44_979
# Haiku at its long-prompt tier, the bound for per-leg usage: $0.50/$2.50, reads
# $0.05, 1h writes $1 per MTok: 2 + 925 + 652 + 14639 micros.
_HAIKU_MICROS = 16_218


def test_a_known_model_is_priced_from_its_tokens_at_the_harness_rate(monkeypatch):
    result, _ = _leg(monkeypatch, _ResultMessage({SONNET: _FRESH[SONNET]}, total_cost_usd=0.05))

    assert result.cost_usd == _SONNET_MICROS / 1e6
    assert isinstance(result.run_usage, MeasuredRunUsage)


def test_a_multi_model_leg_prices_each_models_tokens_at_its_own_rate(monkeypatch):
    result, _ = _leg(monkeypatch, _ResultMessage(_FRESH, total_cost_usd=0.052))

    assert result.cost_usd == (_SONNET_MICROS + _HAIKU_MICROS) / 1e6
    assert {s.model for s in result.run_usage.samples} == {SONNET, HAIKU}


def test_a_resumed_leg_without_a_baseline_is_bounded_by_its_own_and_other_models_usage(monkeypatch):
    envelope = _ResultMessage(_FRESH, _usage(2, 7, 20_348, 438), total_cost_usd=0.058)
    plan = RuntimePlan(prompt="x", model=SONNET, max_budget_usd=10, resume="s1")

    result, lines = _leg(monkeypatch, envelope, plan)

    # The main loop's own tokens (4 + 70 + 2035 + 1752 micros), not its session
    # total; the Haiku model at its session-cumulative usage, an upper bound.
    assert result.cost_usd == (3_861 + _HAIKU_MICROS) / 1e6
    assert isinstance(result.run_usage, EstimatedRunUsage)
    assert not any("CROSS-CHECK" in line for line in lines)  # a cumulative figure compares nothing


def test_a_resumed_leg_that_delegated_is_bounded_by_all_session_cumulative_usage(monkeypatch):
    # Its subagents may have run on the main model, which its own usage omits.
    envelope = _ResultMessage(_FRESH, _usage(2, 7, 20_348, 438))
    plan = RuntimePlan(prompt="x", model=SONNET, max_budget_usd=10, resume="s1")

    result, _ = _leg(monkeypatch, envelope, plan, before=(_subagent_started(),))

    assert result.cost_usd == (_SONNET_MICROS + _HAIKU_MICROS) / 1e6
    assert isinstance(result.run_usage, EstimatedRunUsage)


def test_a_resumed_alias_plan_never_charges_its_main_loop_twice(monkeypatch):
    envelope = _ResultMessage(_FRESH, _usage(2, 7, 20_348, 438))
    plan = RuntimePlan(prompt="x", model="sonnet", max_budget_usd=10, resume="s1")

    result, _ = _leg(monkeypatch, envelope, plan)

    assert result.cost_usd == (3_861 + _HAIKU_MICROS) / 1e6


def test_a_malformed_top_level_usage_a_fresh_leg_does_not_price_from_is_ignored(monkeypatch):
    fresh, _ = _leg(monkeypatch, _ResultMessage({SONNET: _FRESH[SONNET]}, {"input_tokens": None}))

    assert fresh.cost_usd == _SONNET_MICROS / 1e6


def test_a_model_entry_with_no_tokens_needs_no_rate(monkeypatch):
    result, lines = _leg(monkeypatch, _ResultMessage({SONNET: _FRESH[SONNET], "<synthetic>": _model(0, 0, 0, 0)}))

    assert result.cost_usd == _SONNET_MICROS / 1e6
    assert not lines


# Only a CLI figure over 3x ours warns: below it lie the card's deliberate upper bounds.
@pytest.mark.parametrize("reported,warned", [(6.89, True), (0.2, True), (0.05, False), (0.0002, False), (0.0, False)])
def test_a_cli_figure_far_above_the_tokens_price_logs_a_warning_and_never_settles(monkeypatch, reported, warned):
    result, lines = _leg(monkeypatch, _ResultMessage({SONNET: _FRESH[SONNET]}, total_cost_usd=reported))

    assert result.cost_usd == _SONNET_MICROS / 1e6
    assert any("PRICE CROSS-CHECK" in line for line in lines) is warned


@pytest.mark.parametrize(
    "model_usage",
    [
        "modelUsage",  # not a mapping
        [_model(1, 1, 1, 1)],
        {SONNET: None},
        {SONNET: {"inputTokens": 1, "outputTokens": 1, "cacheReadInputTokens": 1}},  # a counter missing
        *({SONNET: _model(bad, 1, 1, 1)} for bad in ("12", 1.5, True, -1, None, 10**12 + 1, 10**100)),
        {f"claude-model-{i}": _model(1, 1, 1, 1) for i in range(65)},  # more models than any leg runs
        {"x" * 300: _model(1, 1, 1, 1)},  # unbounded model name
        {SONNET: _model(1, 1, 1, 1), SONNET.upper(): _model(1, 1, 1, 1)},  # one model charged twice
    ],
)
def test_malformed_absent_or_huge_usage_settles_the_reserved_maximum(monkeypatch, model_usage):
    result, lines = _leg(monkeypatch, _ResultMessage(model_usage, total_cost_usd=0.01))

    assert result.cost_usd == 10.0
    assert isinstance(result.run_usage, EstimatedRunUsage)
    assert any("PRICING" in line and "reserved maximum" in line for line in lines)


def test_absent_usage_is_charged_zero_with_a_gap_and_an_empty_one_is_free(monkeypatch):
    absent, lines = _leg(monkeypatch, _ResultMessage(None, total_cost_usd=0.5))
    # An API error before any request completes reports no models at all.
    empty, _ = _leg(monkeypatch, _ResultMessage({}, total_cost_usd=0.0, is_error=True, subtype="success"))

    assert (absent.cost_usd, empty.cost_usd) == (0.0, 0.0)
    assert isinstance(absent.run_usage, EstimatedRunUsage) and isinstance(empty.run_usage, MeasuredRunUsage)
    assert any("reported no usage" in line for line in lines)


def test_one_absent_usage_field_falls_back_to_the_other_as_an_estimate(monkeypatch):
    resumed, _ = _leg(monkeypatch, _ResultMessage({SONNET: _FRESH[SONNET]}, None),
                      RuntimePlan(prompt="x", model=SONNET, max_budget_usd=10, resume="s1"))
    fresh, _ = _leg(monkeypatch, _ResultMessage(None, _usage(6, 203, 50_407, 9_474)))

    # Resumed without its own usage: the session-cumulative figure bounds it.
    # Fresh without per-model usage: its main loop is all that is known.
    assert resumed.cost_usd == fresh.cost_usd == _SONNET_MICROS / 1e6
    assert isinstance(resumed.run_usage, EstimatedRunUsage) and isinstance(fresh.run_usage, EstimatedRunUsage)


@pytest.mark.parametrize("usage", [{"input_tokens": 1}, "usage", _usage("4", 1, 1, 1), _usage(10**13, 1, 1, 1)])
def test_a_resumed_leg_with_malformed_own_usage_settles_the_reserved_maximum(monkeypatch, usage):
    plan = RuntimePlan(prompt="x", model=SONNET, max_budget_usd=10, resume="s1")

    result, _ = _leg(monkeypatch, _ResultMessage({SONNET: _FRESH[SONNET]}, usage), plan)

    assert result.cost_usd == 10.0


def test_the_counter_bound_itself_is_priced_exactly(monkeypatch):
    result, _ = _leg(monkeypatch, _ResultMessage({HAIKU: _model(10**12, 0, 0, 0)}),
                     RuntimePlan(prompt="x", model=HAIKU, max_budget_usd=10))

    assert result.cost_usd == 500_000.0  # 10^12 tokens at $0.50/MTok


# ── admission and settlement through the accounting boundary ─────────────────


class _Runtime:
    def __init__(self, result):
        self.result, self.calls = result, 0

    async def run(self, _task):
        self.calls += 1
        return self.result


def _accounting(tmp_path, inner, repository="o/r"):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    wrapper = AccountingRuntime(
        inner, store, repository, "fix", Money("USD", 50_000_000), None, None, "cfg", "claude",
        BudgetPolicy({("fix", "RESOLVE"): Money("USD", 10_000_000)}), CLAUDE,
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1)
    store.lease(repository, "fix", 1, wrapper._owner())
    return wrapper, store


def _ended(store):
    with store._read() as db:
        return [tuple(row) for row in db.execute("SELECT charged_micros, basis, rate_card_id FROM legs "
                                                 "WHERE state='ended'")]


@pytest.mark.parametrize("model", ["claude-opus-6", "gpt-6.1-sol-typo"])
def test_a_model_with_no_harness_rate_is_refused_admission_before_anything_runs(tmp_path, model):
    inner = _Runtime(AgentResult(False, status="ok"))
    wrapper, _ = _accounting(tmp_path, inner)

    result = asyncio.run(wrapper.run(RuntimePlan(prompt="x", model=model, max_budget_usd=10)))

    assert inner.calls == 0
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert "no harness rate" in result.summary


# An unpinned plan (None) is priced at settlement from the model the CLI names.
@pytest.mark.parametrize("model", [SONNET, "opus", "Claude-Haiku-5-5", None])
def test_a_rated_model_or_cli_alias_is_admitted(tmp_path, model):
    inner = _Runtime(AgentResult(False, status="ok"))
    wrapper, _ = _accounting(tmp_path, inner)

    asyncio.run(wrapper.run(RuntimePlan(prompt="x", model=model, max_budget_usd=10)))

    assert inner.calls == 1


def test_a_rated_leg_settles_its_token_price_on_the_rate_card(tmp_path, monkeypatch):
    priced, _ = _leg(monkeypatch, _ResultMessage(_FRESH, total_cost_usd=6.89))
    wrapper, store = _accounting(tmp_path, _Runtime(priced))

    settled = asyncio.run(wrapper.run(RuntimePlan(prompt="x", model=SONNET, max_budget_usd=10)))

    assert settled.cost_usd == (_SONNET_MICROS + _HAIKU_MICROS) / 1e6
    assert _ended(store) == [(_SONNET_MICROS + _HAIKU_MICROS, "configured_rate_card", "claude-list-rates")]


def test_an_interrupted_leg_prices_its_stream_tokens_at_the_same_table(tmp_path):
    interrupted = AgentResult(True, status="timeout", exit_reason=ExitReason.WALL_TIMEOUT, cost_usd=5.0,
                              stream_usage={"input_tokens": 1_000_000, "output_tokens": 0})
    wrapper, store = _accounting(tmp_path, _Runtime(interrupted))

    asyncio.run(wrapper.run(RuntimePlan(prompt="x", model=HAIKU, max_budget_usd=10)))

    # $0.50 for a million Haiku input tokens; the runtime's $5.00 is never priced.
    assert _ended(store) == [(500_000, "estimate", "claude-list-rates")]


def test_an_interrupted_leg_on_a_model_with_no_rate_settles_the_reserved_maximum(tmp_path):
    # Unpinned, so admitted; the init named a model newer than the card.
    interrupted = AgentResult(True, status="timeout", exit_reason=ExitReason.WALL_TIMEOUT, model="claude-opus-6",
                              stream_usage={"input_tokens": 1_000_000, "output_tokens": 100_000})
    wrapper, store = _accounting(tmp_path, _Runtime(interrupted))

    asyncio.run(wrapper.run(RuntimePlan(prompt="x", max_budget_usd=10)))

    assert _ended(store) == [(10_000_000, "estimate", "claude-list-rates")]


# ── a session's legs priced by the delta of its cumulative usage ──────────────
# Real envelopes from CLI 2.1.294: a fresh Sonnet leg whose Haiku subagent made
# four requests (r6), then the same task on the resumed session (r7).
_R6 = {SONNET: _model(4, 293, 32_374, 7_721), HAIKU: _model(8, 491, 42_580, 14_969)}
_R7 = {SONNET: _model(8, 539, 73_696, 10_734), HAIKU: _model(16, 903, 98_247, 16_895)}
# r6: Sonnet 8 + 2930 + 3238 + 30884, Haiku 4 + 1228 + 2129 + 14969 micros.
_R6_MICROS = 55_390
# r7 less r6: Sonnet 4/246/41,322/3,013 (= r7's own `usage`) at 8 + 2460 + 4133
# + 12052; Haiku 8/412/55,667/1,926 at 4 + 1030 + 2784 + 1926 micros.
_R7_OWN_MICROS = 24_397


def _session(tmp_path, monkeypatch, repository="o/r"):
    """Run legs of one session through accounting over a real AgentRunner."""
    wrapper, store = _accounting(tmp_path, AgentRunner(rate_table=CLAUDE), repository)

    def leg(*messages, resume="s1"):
        async def query(prompt, options):
            for message in messages:
                yield message

        fake = types.ModuleType("claude_agent_sdk")
        fake.ClaudeAgentOptions = lambda **_kw: object()
        fake.ResultMessage = _ResultMessage
        fake.query = query
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
        store.unlease(repository, "fix", 1, wrapper._owner())
        store.lease(repository, "fix", 1, wrapper._owner())
        return asyncio.run(wrapper.run(RuntimePlan(prompt="x", model=SONNET, max_budget_usd=10, resume=resume)))

    return leg, store


def _charges(store):
    return [(micros, basis) for micros, basis, _card in _ended(store)]


def test_a_resumed_leg_with_a_subagent_is_priced_exactly_by_the_session_delta(tmp_path, monkeypatch):
    leg, store = _session(tmp_path, monkeypatch)

    leg(_ResultMessage(_R6, _usage(4, 293, 32_374, 7_721)), resume=None)
    resumed = leg(_subagent_started(), _ResultMessage(_R7, _usage(4, 246, 41_322, 3_013), total_cost_usd=0.11))

    assert _charges(store) == [(_R6_MICROS, "configured_rate_card"), (_R7_OWN_MICROS, "configured_rate_card")]
    assert isinstance(resumed.run_usage, MeasuredRunUsage)


def test_the_resumed_issue_1078_leg_settles_its_own_tokens_not_the_session_total(tmp_path, monkeypatch):
    # FIX_CI on tauri #1078: the CLI reported $8.14, the session's running total;
    # the leg's own usage was 4 in, 1,443 out, 284,614 read, 29,637 written.
    leg, store = _session(tmp_path, monkeypatch)
    leg(_ResultMessage({SONNET: _model(56, 38_557, 3_715_386, 570_363)}), resume=None)

    leg(_ResultMessage({SONNET: _model(60, 40_000, 4_000_000, 600_000)}, _usage(4, 1_443, 284_614, 29_637),
                       total_cost_usd=8.14))

    # 8 + 14430 + 28462 + 118548 micros.
    assert _charges(store)[1] == (161_448, "configured_rate_card")


def test_a_resumed_leg_with_no_baseline_settles_its_upper_bound(tmp_path, monkeypatch):
    leg, store = _session(tmp_path, monkeypatch)

    resumed = leg(_ResultMessage(_R7, _usage(4, 246, 41_322, 3_013)))

    # Its own Sonnet usage (18,653 micros) plus Haiku's session-cumulative usage
    # (8 + 2258 + 4913 + 16895): above the exact 24,397, never below.
    assert _charges(store) == [(18_653 + 24_074, "estimate")]
    assert "no usage snapshot" in resumed.run_usage.detail


def test_an_interrupted_previous_leg_is_bounded_by_charging_its_tokens_again(tmp_path, monkeypatch):
    leg, store = _session(tmp_path, monkeypatch)
    leg(_ResultMessage(_R6, _usage(4, 293, 32_374, 7_721)), resume=None)
    streamed = types.SimpleNamespace(model=SONNET, usage={"input_tokens": 2, "output_tokens": 0,
                                                         "cache_read_input_tokens": 20_000,
                                                         "cache_creation_input_tokens": 0})
    leg(streamed)  # no result: its stream estimate is charged, and it leaves no snapshot

    leg(_ResultMessage(_R7, _usage(4, 246, 41_322, 3_013)))

    # The next leg's delta runs from r6, so it includes the interrupted leg's
    # tokens again: an overcharge bounded by what that leg spent.
    assert _charges(store) == [(_R6_MICROS, "configured_rate_card"), (2_004, "estimate"),
                               (_R7_OWN_MICROS, "configured_rate_card")]


def test_counters_that_went_backwards_settle_the_upper_bound(tmp_path, monkeypatch):
    leg, store = _session(tmp_path, monkeypatch)
    leg(_ResultMessage(_R7, _usage(8, 539, 73_696, 10_734)), resume=None)

    resumed = leg(_ResultMessage(_R6, _usage(4, 293, 32_374, 7_721)))

    # Its own Sonnet usage plus Haiku's cumulative usage: r6's price.
    assert _charges(store)[1] == (_R6_MICROS, "estimate")
    assert "were reset" in resumed.run_usage.detail


def test_a_leg_that_ended_unresumable_still_leaves_the_sessions_baseline(tmp_path, monkeypatch):
    # A leg that exhausted its budget is not resumed itself, but its turns are
    # in the session the next leg resumes: they are charged once.
    leg, store = _session(tmp_path, monkeypatch)
    leg(_ResultMessage(_R6, _usage(4, 293, 32_374, 7_721)), resume=None)
    leg(_ResultMessage(_R7, _usage(4, 246, 41_322, 3_013), subtype="error_max_budget_usd", is_error=True))
    third = {SONNET: _model(12, 700, 100_000, 12_000), HAIKU: _R7[HAIKU]}

    leg(_ResultMessage(third, _usage(4, 161, 26_304, 1_266)))

    # Sonnet 4/161/26,304/1,266: 8 + 1610 + 2630.4 + 5064 micros, rounded up.
    assert _charges(store)[2] == (9_313, "configured_rate_card")


def test_counters_restored_stale_are_a_reset_not_a_cheap_leg(tmp_path, monkeypatch):
    # The CLI restores cumulative counters from its transcript; a lost entry
    # restarts them, and a larger leg would still pass a backwards check.
    leg, store = _session(tmp_path, monkeypatch)
    leg(_ResultMessage(_R6, _usage(4, 293, 32_374, 7_721)), resume=None)
    restarted = {SONNET: _model(10, 600, 80_000, 9_000), HAIKU: _model(10, 600, 50_000, 15_000)}

    resumed = leg(_ResultMessage(restarted, _usage(10, 600, 80_000, 9_000)))

    # Its own Sonnet usage (20 + 6000 + 8000 + 36000) plus all of Haiku's
    # (5 + 1500 + 2500 + 15000): the bound, not the 2,947-micro difference.
    assert _charges(store)[1] == (69_025, "estimate")
    assert "were reset" in resumed.run_usage.detail


def test_a_snapshot_from_another_session_or_repository_is_no_baseline(tmp_path, monkeypatch):
    other_repo, _ = _session(tmp_path, monkeypatch, repository="o/other")
    other_repo(_ResultMessage(_R6, _usage(4, 293, 32_374, 7_721)), resume=None)
    leg, store = _session(tmp_path, monkeypatch)
    leg(_ResultMessage(_R6, _usage(4, 293, 32_374, 7_721), session_id="s2"), resume=None)

    resumed = leg(_ResultMessage(_R7, _usage(4, 246, 41_322, 3_013)))

    assert "no usage snapshot" in resumed.run_usage.detail


def test_the_latest_of_the_sessions_snapshots_is_the_baseline(tmp_path, monkeypatch):
    leg, store = _session(tmp_path, monkeypatch)
    leg(_ResultMessage(_R6, _usage(4, 293, 32_374, 7_721)), resume=None)
    leg(_ResultMessage(_R7, _usage(4, 246, 41_322, 3_013)))

    leg(_ResultMessage({**_R7, SONNET: _model(9, 539, 73_696, 10_734)}, _usage(1, 0, 0, 0)))

    assert _charges(store)[2] == (2, "configured_rate_card")


def test_an_unreadable_baseline_settles_the_upper_bound(tmp_path, monkeypatch):
    leg, store = _session(tmp_path, monkeypatch)
    leg(_ResultMessage(_R6, _usage(4, 293, 32_374, 7_721)), resume=None)

    def broken(*_args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "session_usage_snapshot", broken)
    resumed = leg(_ResultMessage(_R7, _usage(4, 246, 41_322, 3_013)))

    assert _charges(store)[1] == (18_653 + 24_074, "estimate")
    assert "delta is unavailable" in resumed.run_usage.detail


def test_a_resumed_leg_with_malformed_per_model_usage_settles_the_reserved_maximum(tmp_path, monkeypatch):
    leg, store = _session(tmp_path, monkeypatch)
    leg(_ResultMessage(_R6, _usage(4, 293, 32_374, 7_721)), resume=None)

    leg(_ResultMessage({SONNET: _model("8", 539, 73_696, 10_734)}, _usage(4, 246, 41_322, 3_013)))

    assert _charges(store)[1] == (10_000_000, "estimate")


_GOOD = {SONNET: [1, 1, 1, 1]}


@pytest.mark.parametrize("snapshot", [
    None, {}, "snapshot", {"session_id": "s1"}, {"session_id": "s2", "models": _GOOD}, {"models": _GOOD},
    *({"session_id": "s1", "models": models} for models in (
        {}, [[1, 1, 1, 1]], {SONNET: [1, 1, 1]}, {SONNET: [1, 1, 1, -1]}, {SONNET: [1, 1, 1, True]},
        {SONNET: [1, 1, 1, 10**13]}, {SONNET: [1, 1, 1, 1], SONNET.upper(): [1, 1, 1, 1]},
        {f"m{i}": [1, 1, 1, 1] for i in range(65)})),
])
def test_a_malformed_huge_or_foreign_recorded_snapshot_is_no_baseline(snapshot):
    assert parse_snapshot("s1", snapshot) is None


def test_a_recorded_snapshot_round_trips():
    samples = parse_snapshot("s1", {"session_id": "s1", "models": {SONNET: [4, 293, 32_374, 7_721],
                                                                   HAIKU: [8, 491, 42_580, 14_969]}})
    assert samples is not None and parse_snapshot("s1", snapshot_record("s1", samples)) == samples


def test_tokens_from_a_model_with_no_harness_rate_settle_the_reserved_maximum(tmp_path, monkeypatch):
    # A stale CLI prices an unknown model at a fallback; the harness trusts neither.
    leg, store = _session(tmp_path, monkeypatch)

    result = leg(_ResultMessage({SONNET: _FRESH[SONNET], "claude-haiku-6": _model(1, 1, 1, 1)}), resume=None)

    assert _charges(store) == [(10_000_000, "estimate")]
    assert "no harness rate for claude-haiku-6" in result.run_usage.detail


def test_a_resumed_leg_prices_the_concrete_model_its_alias_resolved_to(tmp_path, monkeypatch):
    # The init names what the CLI resolved the plan's model to; a model the
    # card does not know is never priced at another model's rate.
    leg, store = _session(tmp_path, monkeypatch)
    init = types.SimpleNamespace(subtype="init", data={"model": "claude-sonnet-6", "session_id": "s1"})

    leg(init, _ResultMessage({"claude-sonnet-6": _model(1, 1, 1, 1)}, _usage(1, 1, 1, 1)))

    assert _charges(store) == [(10_000_000, "estimate")]


# ── the rate card ─────────────────────────────────────────────────────────────


def test_the_claude_card_carries_published_list_rates_and_trusts_no_fallback():
    assert CLAUDE is not None
    assert CLAUDE.unknown_model_policy is UnknownModelPolicy.REJECT and CLAUDE.fallback is None
    opus = CLAUDE.rates_for("claude-opus-5-5")
    assert (opus.input_micros, opus.output_micros, opus.cache_read_micros, opus.cache_write_micros) == (
        4_000_000, 20_000_000, 200_000, 8_000_000)
    fable = CLAUDE.rates_for("claude-fable-5-1")
    assert (fable.input_micros, fable.cache_read_micros, fable.cache_write_micros) == (10_000_000, 250_000, 20_000_000)
    assert not CLAUDE.priceable("claude-opus-6")


def test_an_operator_row_prices_a_model_newer_than_the_shipped_rates():
    card = rate_table(from_env({"CODEX_COST_PER_MTOK": "claude-opus-6=6/30/0.3,claude-haiku-5-5=1/5"}), "claude")
    assert card is not None
    new = card.rates_for("claude-opus-6")
    assert (new.input_micros, new.output_micros, new.cache_read_micros, new.cache_write_micros) == (
        6_000_000, 30_000_000, 300_000, 12_000_000)
    assert card.rates_for("claude-haiku-5-5").input_micros == 1_000_000  # the operator's row wins


def test_an_operator_override_of_a_model_reprices_its_alias():
    card = rate_table(from_env({"CODEX_COST_PER_MTOK": "claude-sonnet-5-5=3/15"}), "claude")
    assert card is not None and card.rates_for("sonnet") == card.rates_for("claude-sonnet-5-5")
    assert card.rates_for("sonnet").input_micros == 3_000_000


@pytest.mark.parametrize("env,unrated", [
    ({"FIX_MODEL": "claude-opus-6"}, "claude-opus-6"),
    ({"FIX_MODEL_HIGH": "claude:claude-sonnet-6"}, "claude-sonnet-6"),
    ({"REVIEWER_RUNTIME": "claude", "REVIEW_MODEL": "claude-haiku-6"}, "claude-haiku-6"),
    ({"FIX_MODEL": "opus", "TRIAGE_MODEL": "claude:claude-haiku-5-5"}, None),
    ({"FIX_MODEL": "claude-opus-6", "CODEX_COST_PER_MTOK": "claude-opus-6=6/30"}, None),
    ({"FIX_MODEL": "claude-opus-6", "ISSUE_SPEND_CAP_USD": "0", "DAILY_SPEND_CAP_USD": "0"}, None),
])
def test_armed_caps_refuse_a_configured_claude_model_with_no_rate_at_startup(env, unrated):
    errors = [e for e in from_env({"REPO": "o/r", **env}).validation_errors() if "harness rate" in e]

    assert (unrated in errors[0]) if unrated else errors == []


def test_the_claude_card_does_not_depend_on_the_codex_scalars():
    card = rate_table(from_env({"CODEX_COST_PER_MTOK_IN": "0", "CODEX_COST_PER_MTOK_OUT": "0"}), "claude")
    assert card is not None and card.priceable(SONNET)
