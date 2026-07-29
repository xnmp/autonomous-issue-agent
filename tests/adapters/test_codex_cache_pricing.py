"""Regression coverage for cache-aware Codex accounting (#89)."""

import pytest

from issue_agent.adapters.codex_runner import budget_overrun_usd, codex_cost_usd
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
