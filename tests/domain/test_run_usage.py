"""The charge includes each actually observed model at its own rate."""

from dataclasses import replace

import pytest

from issue_agent.domain.contracts import Money, TokenUsage, UsageSource
from issue_agent.domain.pricing import ModelRates, RateTable
from issue_agent.domain.run_usage import ModelUsage, MeasuredRunUsage, UnavailableRunUsage, price_run_usage, total_usage


def test_mixed_models_price_disjoint_usage_with_one_currency_and_conservative_rounding():
    observed = MeasuredRunUsage((
        ModelUsage("parent", "low", TokenUsage(1, 2, 3, 4, UsageSource.ROLLOUT)),
        ModelUsage("review", "high", TokenUsage(1, 2, 3, 4, UsageSource.ROLLOUT)),
    ))
    table = RateTable("card", "v1", "USD", {
        "low": ModelRates(100000, 500000, 10000, 125000),
        "high": ModelRates(2000000, 10000000, 100000, 2500000),
    })
    assert price_run_usage(observed, table) == Money("USD", 37)
    total = total_usage(observed.samples)
    assert (total.input_tokens, total.output_tokens, total.cache_read_tokens, total.cache_write_tokens) == (2, 4, 6, 8)


@pytest.mark.parametrize("invalid", [(), [], (object(),)])
def test_empty_or_malformed_samples_cannot_claim_measured_free_work(invalid):
    with pytest.raises(ValueError):
        MeasuredRunUsage(invalid)


def test_case_insensitive_duplicate_models_cannot_be_charged_twice():
    sample = ModelUsage("parent", "LOW", TokenUsage(1, 0))
    with pytest.raises(ValueError):
        MeasuredRunUsage((sample, replace(sample, model=" low ")))


@pytest.mark.parametrize("usage", [
    TokenUsage(1, 0, source=UsageSource.STDERR),
    TokenUsage(1, 0, source="rollout"),
    TokenUsage(True, 0),
    TokenUsage(1.5, 0),
])
def test_invalid_provenance_or_counters_cannot_become_pricing_authority(usage):
    with pytest.raises(ValueError):
        ModelUsage("parent", "low", usage)


def test_unknown_model_or_incomplete_collection_never_prices_the_known_subset_as_complete():
    observed = MeasuredRunUsage((ModelUsage("parent", "unknown", TokenUsage(1, 0)),))
    table = RateTable("card", "v1", "USD", {"known": ModelRates(1, 1, 1, 1)})
    with pytest.raises(KeyError):
        price_run_usage(observed, table)
    with pytest.raises(ValueError):
        price_run_usage(UnavailableRunUsage("child receipt missing", observed.samples), table)


def test_large_trusted_values_preserve_exact_arithmetic():
    observed = MeasuredRunUsage((ModelUsage("parent", "high", TokenUsage(10**30, 0)),))
    table = RateTable("card", "v1", "USD", {"high": ModelRates(2_000_000, 10_000_000, 100_000, 2_500_000)})
    assert price_run_usage(observed, table).micros == 2 * 10**30

