"""Regression coverage for cache-aware Codex accounting (#89)."""

from dataclasses import replace

import pytest

from issue_agent.adapters.codex_runner import (
    budget_overrun_usd,
    codex_budget_not_enforced_warning,
    codex_cost_blindspot_warning,
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


def test_the_pricing_gap_names_the_cached_knob_not_the_in_out_scalars():
    runtime = _cached_gap_runtime()
    assert runtime.budget_priceable is False
    assert "CACHED_IN" in runtime.budget_pricing_gap
    assert runtime.unpriced_cached_models == ("gpt-5.6-sol",)

    warning = codex_budget_not_enforced_warning(
        fix_budget_usd=20.0,
        max_runtime=3600.0,
        enforced=True,
        priced=runtime.budget_priceable,
        pricing_gap=runtime.budget_pricing_gap,
    )
    assert warning is not None
    assert "CACHED_IN" in warning
    assert "CODEX_COST_PER_MTOK_IN and" not in warning, (
        "two knobs that are already correct"
    )


def test_the_blindspot_line_fires_for_an_unpriced_cache_tier():
    runtime = _cached_gap_runtime()
    warning = codex_cost_blindspot_warning(
        cost_per_mtok_in=runtime.cost_per_mtok_in,
        cost_per_mtok_out=runtime.cost_per_mtok_out,
        issue_spend_cap_usd=40.0,
        daily_spend_cap_usd=0.0,
        max_runtime=3600.0,
        unpriced_cached_models=runtime.unpriced_cached_models,
    )
    assert warning is not None
    assert "CACHED_IN" in warning and "gpt-5.6-sol" in warning
    assert "cache reads price at $0.00" in warning


def test_the_blindspot_line_stays_silent_on_a_fully_priced_cache_tier():
    cfg = from_env({
        "REPO": "x", "FIX_BOT": "fix-bot",
        "CODEX_COST_PER_MTOK": "gpt-5.6-sol=5/30/0.5",
        "ISSUE_SPEND_CAP_USD": "40",
    })
    assert cfg.codex_runtime.budget_priceable is True
    assert codex_cost_blindspot_warning(
        cost_per_mtok_in=cfg.codex_runtime.cost_per_mtok_in,
        cost_per_mtok_out=cfg.codex_runtime.cost_per_mtok_out,
        issue_spend_cap_usd=cfg.issue_spend_cap_usd,
        daily_spend_cap_usd=cfg.daily_spend_cap_usd,
        unpriced_cached_models=cfg.codex_runtime.unpriced_cached_models,
    ) is None
