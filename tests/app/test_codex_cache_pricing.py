"""Regression coverage for cache-aware Codex accounting (#89)."""

from dataclasses import replace

import pytest

from issue_agent.adapters.codex_runner import (
    budget_overrun_usd,
    codex_cost_usd,
    rates_with_cache_for_model,
)
from issue_agent.app.config import from_env


ISSUE_71_USAGE = {
    "input_tokens": 3_974_000,
    "cache_read_input_tokens": 3_773_000,
    "output_tokens": 9_500,
}


def test_model_cache_rate_prices_the_issue_71_usage_and_keeps_it_under_budget():
    cfg = from_env({
        "REPO": "x", "FIX_BOT": "fix-bot",
        "CODEX_COST_PER_MTOK": "gpt-5.6-sol=5/30/0.5",
    })
    assert cfg.codex_runtime.cost_per_mtok_cached_in == (("gpt-5.6-sol", 0.5),)
    cost = codex_cost_usd(
        ISSUE_71_USAGE,
        cost_per_mtok_in=5.0,
        cost_per_mtok_cached_in=0.5,
        cost_per_mtok_out=30.0,
    )
    assert cost == pytest.approx(3.1765)
    assert budget_overrun_usd(
        ISSUE_71_USAGE,
        cost_per_mtok_in=5.0,
        cost_per_mtok_cached_in=0.5,
        cost_per_mtok_out=30.0,
        max_budget_usd=20.0,
    ) is None


@pytest.mark.parametrize("cache_read", [None, 0])
def test_missing_or_zero_cache_read_tokens_keep_full_input_pricing(cache_read):
    usage = {"input_tokens": 48_000, "output_tokens": 4_341}
    if cache_read is not None:
        usage["cache_read_input_tokens"] = cache_read
    assert codex_cost_usd(
        usage,
        cost_per_mtok_in=1.25,
        cost_per_mtok_cached_in=0.125,
        cost_per_mtok_out=10.0,
    ) == pytest.approx(0.10341)


# ── the cache tier must never read as free ───────────────────────────────────
# A $0 cache rate would price the leg above at $0.29 instead of $20.16, so the
# dollar bound would stop firing on the very legs it exists to stop.

FULL_RATE_ISSUE_71_COST = 20.155  # (3974000 * $5 + 9500 * $30) / 1e6


@pytest.mark.parametrize("configured,expected", [
    ("gpt-5.6-sol=5/30", (10.0, 50.0, 1.0)),
    ("gpt-6-astra=12/60/2", (12.0, 60.0, 2.0)),
])
def test_astra_runtime_and_accounting_use_the_same_rates(configured, expected):
    from issue_agent.app.roles import _rate_table
    from issue_agent.domain.contracts import TokenUsage
    from issue_agent.domain.pricing import price

    cfg = from_env({"CODEX_COST_PER_MTOK": configured})
    rates = rates_with_cache_for_model(
        "gpt-6-astra", cfg.codex_cost_per_mtok, cfg.codex_cost_per_mtok_cached_in,
        fallback_in=cfg.codex_cost_per_mtok_in,
        fallback_out=cfg.codex_cost_per_mtok_out,
    )
    assert rates == expected
    table = _rate_table(cfg, "codex")
    assert table is not None
    charge = price(TokenUsage(1_000_000, 1_000_000, cache_read_tokens=1_000_000),
                   table.rates_for("gpt-6-astra"))
    assert charge.micros == round(sum(expected) * 1_000_000)


def test_rate_card_version_tracks_shipped_prices_without_an_overlay_edit(monkeypatch):
    from issue_agent.app.roles import _rate_table
    from issue_agent.domain import pricing

    cfg = from_env({"CODEX_COST_PER_MTOK": "gpt-5.6-sol=5/30"})
    current = _rate_table(cfg, "codex")
    monkeypatch.setattr(pricing, "CODEX_DEFAULT_MODEL_RATES", (("gpt-6-astra", 12.0, 60.0),))
    changed = _rate_table(cfg, "codex")

    assert current is not None and changed is not None
    assert current.rates_for("gpt-6-astra") != changed.rates_for("gpt-6-astra")
    assert current.version != changed.version
    assert current.rate_card_id == changed.rate_card_id


@pytest.mark.parametrize("raw", [
    "gpt-5.6-sol=5/30/0",       # cache reads would price free
    "gpt-5.6-sol=5/30/-1",      # and negative would pay the leg back
    "gpt-5.6-sol=5/30/abc",     # non-numeric third component
    "gpt-5.6-sol=5//30",        # empty middle: OUT must not become CACHED_IN
    "gpt-5.6-sol=5/30/1/2",     # four components
])
def test_a_non_positive_or_malformed_cache_rate_never_reaches_either_table(raw):
    cfg = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "CODEX_COST_PER_MTOK": raw})
    assert cfg.codex_cost_per_mtok == ()
    assert cfg.codex_cost_per_mtok_cached_in == ()
    assert any(raw in e for e in cfg.validation_errors()), cfg.validation_errors()


def test_a_non_positive_cache_rate_prices_cache_reads_at_the_full_input_rate():
    """Config rejects it; if one reaches the runtime anyway the leg must be
    OVER-estimated, because an under-counted cap is a cap that never fires."""
    for rate in (0.0, -1.0):
        assert codex_cost_usd(
            ISSUE_71_USAGE,
            cost_per_mtok_in=5.0,
            cost_per_mtok_cached_in=rate,
            cost_per_mtok_out=30.0,
        ) == pytest.approx(FULL_RATE_ISSUE_71_COST)
    assert budget_overrun_usd(
        ISSUE_71_USAGE,
        cost_per_mtok_in=5.0,
        cost_per_mtok_cached_in=0.0,
        cost_per_mtok_out=30.0,
        max_budget_usd=20.0,
    ) == pytest.approx(FULL_RATE_ISSUE_71_COST), "enforcement stays armed"


def test_a_zero_table_entry_resolves_to_the_full_input_rate():
    assert rates_with_cache_for_model(
        "gpt-5.6-sol",
        (("gpt-5.6-sol", 5.0, 30.0),),
        (("gpt-5.6-sol", 0.0),),
        fallback_in=1.0,
        fallback_out=2.0,
    ) == (5.0, 30.0, 5.0)


@pytest.mark.parametrize("cache_read", [-1, 100_001, 10**9])
def test_a_cache_counter_the_input_total_cannot_contain_is_distrusted(cache_read):
    """Cache reads are a SUBSET of input_tokens; a counter that breaks that
    cannot be used to discount input the run may never have cached."""
    usage = {
        "input_tokens": 100_000,
        "cache_read_input_tokens": cache_read,
        "output_tokens": 1_000,
    }
    assert codex_cost_usd(
        usage,
        cost_per_mtok_in=5.0,
        cost_per_mtok_cached_in=0.5,
        cost_per_mtok_out=30.0,
    ) == pytest.approx((100_000 * 5.0 + 1_000 * 30.0) / 1e6)


# ── startup reporting must name the rate that is actually missing ────────────


def _cached_gap_runtime():
    """A runtime whose only pricing gap is a $0 cache tier."""
    runtime = from_env({"REPO": "x", "FIX_BOT": "fix-bot"}).codex_runtime
    return replace(
        runtime,
        cost_per_mtok=(("gpt-5.6-sol", 5.0, 30.0),),
        cost_per_mtok_cached_in=(("gpt-5.6-sol", 0.0),),
    )



@pytest.mark.parametrize("model,expected", [
    ("gpt-6.1-sol", (2.0, 10.0, 0.1)),
    ("gpt-6-luna", (0.1, 0.5, 0.01)),
])
def test_new_model_runtime_sentinel_and_accounting_share_effective_prices(model, expected):
    from issue_agent.app.roles import _rate_table
    from issue_agent.domain.contracts import TokenUsage
    from issue_agent.domain.pricing import price
    cfg = from_env({"CODEX_COST_PER_MTOK": "gpt-5.6-sol=5/30/0.5"})
    runtime_rates = rates_with_cache_for_model(model, cfg.codex_cost_per_mtok,
        cfg.codex_cost_per_mtok_cached_in, fallback_in=cfg.codex_cost_per_mtok_in,
        fallback_out=cfg.codex_cost_per_mtok_out)
    assert runtime_rates == pytest.approx(expected)
    usage = {"input_tokens": 2_000_000, "cache_read_input_tokens": 1_000_000,
             "output_tokens": 1_000_000}
    runtime_cost = codex_cost_usd(usage, cost_per_mtok_in=runtime_rates[0],
        cost_per_mtok_out=runtime_rates[1], cost_per_mtok_cached_in=runtime_rates[2])
    table = _rate_table(cfg, "codex")
    assert table is not None
    accounted_cost = price(TokenUsage(1_000_000, 1_000_000, cache_read_tokens=1_000_000), table.rates_for(model))
    assert accounted_cost.micros == round(runtime_cost * 1_000_000)
    assert budget_overrun_usd(usage, cost_per_mtok_in=runtime_rates[0],
        cost_per_mtok_out=runtime_rates[1], cost_per_mtok_cached_in=runtime_rates[2],
        max_budget_usd=runtime_cost - 0.01) == pytest.approx(runtime_cost)


def test_sol_operator_prices_override_shipped_defaults_in_both_accounting_paths():
    from issue_agent.app.roles import _rate_table
    cfg = from_env({"CODEX_COST_PER_MTOK": "gpt-6.1-sol=3/12/0.25"})
    rates = rates_with_cache_for_model("gpt-6.1-sol", cfg.codex_cost_per_mtok,
        cfg.codex_cost_per_mtok_cached_in, fallback_in=5, fallback_out=30)
    assert rates == (3, 12, 0.25)
    table = _rate_table(cfg, "codex")
    assert table is not None
    assert table.rates_for("gpt-6.1-sol").cache_read_micros == 250_000


def test_sol_two_component_operator_rate_uses_its_model_cache_discount():
    cfg = from_env({"CODEX_COST_PER_MTOK": "gpt-6.1-sol=2/10"})
    assert cfg.codex_cost_per_mtok_cached_in == (("gpt-6.1-sol", 0.1),)
    assert rates_with_cache_for_model("gpt-6.1-sol", cfg.codex_cost_per_mtok,
        cfg.codex_cost_per_mtok_cached_in, fallback_in=5, fallback_out=30) == (2, 10, 0.1)
