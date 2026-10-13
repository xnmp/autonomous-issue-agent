from datetime import datetime, timezone

import pytest

from issue_agent.domain.contracts import Money, TokenUsage
from issue_agent.domain.pricing import ModelRates, RateTable, UnknownModelPolicy, parse_model_rate_table, price


RATES = ModelRates(
    input_micros=2_000_000, output_micros=10_000_000, cache_read_micros=200_000, cache_write_micros=2_500_000
)


def test_price_covers_all_disjoint_counters_without_float_drift():
    usage = TokenUsage(1_000_000, 2_000_000, 3_000_000, 4_000_000)
    assert price(usage, RATES) == Money("USD", 32_600_000)
    huge = price(TokenUsage(10**30, 0), RATES)
    assert huge.micros == 2 * 10**30


def test_fractional_micro_rounds_toward_the_cap():
    assert price(TokenUsage(1, 0), RATES) == Money("USD", 2)


def test_rate_quote_locks_identity_and_unknown_model_policy():
    table = RateTable("codex-list", "2026-07-30", "USD", {"gpt": RATES})
    quote = table.quote("GPT", TokenUsage(10, 10), datetime.now(timezone.utc))
    assert quote.rate_card_id == "codex-list"
    assert quote.maximum_charge.micros > 0
    with pytest.raises(KeyError):
        table.rates_for("unknown")
    fallback = RateTable(
        "fallback", "v1", "USD", {}, unknown_model_policy=UnknownModelPolicy.USE_FALLBACK, fallback=RATES
    )
    assert fallback.rates_for("anything") == RATES


def test_rate_table_parser_is_single_source_for_normal_and_cached_tiers():
    normal, cached, errors = parse_model_rate_table("gpt=2/10/0.2, other=1/5, GPT=3/9, malformed")
    assert normal == (("gpt", 2.0, 10.0), ("other", 1.0, 5.0))
    assert cached == (("gpt", 0.2), ("other", 0.1))
    assert len(errors) == 2
