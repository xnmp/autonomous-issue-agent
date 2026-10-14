"""The charge includes each actually observed model at its own rate."""

from dataclasses import replace

import pytest

from issue_agent.domain.contracts import Money, TokenUsage, UsageSource
from issue_agent.domain.pricing import ModelRates, RateTable
from issue_agent.domain.run_usage import ModelUsage, MeasuredRunUsage, EstimatedRunUsage, price_run_usage, total_usage


def test_mixed_models_price_disjoint_usage_with_one_currency_and_conservative_rounding():
    observed = MeasuredRunUsage(
        (
            ModelUsage("parent", "low", TokenUsage(1, 2, 3, 4, UsageSource.ROLLOUT)),
            ModelUsage("review", "high", TokenUsage(1, 2, 3, 4, UsageSource.ROLLOUT)),
        )
    )
    table = RateTable(
        "card",
        "v1",
        "USD",
        {"low": ModelRates(100000, 500000, 10000, 125000), "high": ModelRates(2000000, 10000000, 100000, 2500000)},
    )
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


@pytest.mark.parametrize(
    "usage",
    [
        TokenUsage(1, 0, source=UsageSource.STDERR),
        TokenUsage(1, 0, source="rollout"),
        TokenUsage(True, 0),
        TokenUsage(1.5, 0),
    ],
)
def test_invalid_provenance_or_counters_cannot_become_pricing_authority(usage):
    with pytest.raises(ValueError):
        ModelUsage("parent", "low", usage)


def test_partial_evidence_prices_the_known_subset_and_unknown_requests_cost_zero():
    samples = (ModelUsage("parent", "known", TokenUsage(1, 0)), ModelUsage("child", "unknown", TokenUsage(100, 0)))
    table = RateTable("card", "v1", "USD", {"known": ModelRates(1_000_000, 1, 1, 1)})
    assert price_run_usage(EstimatedRunUsage("child model unpriced", samples), table) == Money("USD", 1)
    assert price_run_usage(EstimatedRunUsage("interrupted request"), table) == Money.zero()


def test_large_trusted_values_preserve_exact_arithmetic():
    observed = MeasuredRunUsage((ModelUsage("parent", "high", TokenUsage(10**30, 0)),))
    table = RateTable("card", "v1", "USD", {"high": ModelRates(2_000_000, 10_000_000, 100_000, 2_500_000)})
    assert price_run_usage(observed, table).micros == 2 * 10**30


def test_model_switches_within_the_session_bound_preserve_every_charge():
    samples = tuple(ModelUsage(f"session-{i // 2}", f"model-{i}", TokenUsage(1, 0)) for i in range(66))
    table = RateTable("card", "v1", "USD", {f"model-{i}": ModelRates(1_000_000, 1, 1, 1) for i in range(66)})
    assert price_run_usage(MeasuredRunUsage(samples), table) == Money("USD", 66)


def test_fixed_charges_and_frozen_token_prices_share_one_total():
    samples = (
        ModelUsage("reported", "no-card-model", TokenUsage(100, 0), Money("USD", 700)),
        ModelUsage("estimated", "known", TokenUsage(100, 0)),
    )
    table = RateTable("card", "v1", "USD", {"known": ModelRates(2_000_000, 1, 1, 1)})
    assert price_run_usage(EstimatedRunUsage("fixed charge", samples), table) == Money("USD", 900)
    with pytest.raises(ValueError, match="currency"):
        price_run_usage(EstimatedRunUsage("fixed", (replace(samples[0], reported_cost=Money("EUR", 700)),)), table)


def test_measured_usage_is_priced_from_tokens_never_a_fixed_charge():
    # A fixed charge (the reserved maximum) is an estimate by construction (#389).
    with pytest.raises(ValueError, match="fixed charge"):
        MeasuredRunUsage((ModelUsage("s", "m", TokenUsage(100, 0), Money("USD", 700)),))


def test_same_session_and_model_keep_per_request_prices_and_rounding():
    samples = tuple(ModelUsage("parent", "low", TokenUsage(1, 0), request_id=name) for name in ("a", "b"))
    table = RateTable("card", "v1", "USD", {"low": ModelRates(100_000, 1, 1, 1)})
    assert price_run_usage(MeasuredRunUsage(samples), table) == Money("USD", 2)
    assert price_run_usage(
        EstimatedRunUsage("fixed", (replace(samples[0], reported_cost=Money("USD", 900)), samples[1])), table
    ) == Money("USD", 901)
    assert total_usage(samples).input_tokens == 2
    with pytest.raises(ValueError, match="unique"):
        MeasuredRunUsage((samples[0], replace(samples[0], model="other")))


@pytest.mark.parametrize("request_id", [None, True, 123, "x" * 257])
def test_malformed_request_identity_cannot_become_a_pricing_sample(request_id):
    with pytest.raises(ValueError, match="request identity"):
        ModelUsage("parent", "low", TokenUsage(1, 0), request_id=request_id)


def test_frozen_observation_rates_keep_live_and_terminal_prices_equal_when_caller_rates_change():
    frozen = RateTable("frozen", "v1", "USD", {"low": ModelRates(2_000_000, 1, 1, 1)})
    changed = RateTable("changed", "v2", "USD", {"low": ModelRates(5_000_000, 1, 1, 1)})
    samples = (
        ModelUsage("parent", "low", TokenUsage(100, 0), Money("USD", 900), "receipt"),
        ModelUsage("parent", "low", TokenUsage(100, 0)),
    )
    for observed in (MeasuredRunUsage(samples[1:], frozen), EstimatedRunUsage("remainder", samples, frozen)):
        assert price_run_usage(observed, None) == price_run_usage(observed, changed)
    assert price_run_usage(EstimatedRunUsage("remainder", samples, frozen), changed) == Money("USD", 1100)


@pytest.mark.parametrize("rates", [True, "card", {}])
def test_unvalidated_prices_cannot_become_frozen_run_evidence(rates):
    samples = (ModelUsage("parent", "low", TokenUsage(1, 0)),)
    with pytest.raises(ValueError, match="frozen rate table"):
        MeasuredRunUsage(samples, rates)
    with pytest.raises(ValueError, match="frozen rate table"):
        EstimatedRunUsage("gap", samples, rates)
