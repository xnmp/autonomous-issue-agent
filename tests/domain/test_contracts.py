from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from issue_agent.domain.contracts import (
    BudgetScope,
    Money,
    PriceQuote,
    PricingBasis,
    RunId,
    SettlementRequest,
    TokenUsage,
    UsageSource,
    daily_budget_scope,
)


def test_money_is_integer_currency_safe_arithmetic():
    assert Money("USD", 7) + Money("USD", 5) == Money("USD", 12)
    with pytest.raises(ValueError, match="different currencies"):
        Money("USD", 7) + Money("AUD", 5)
    with pytest.raises(ValueError, match="negative"):
        Money("USD", -1)


@pytest.mark.parametrize("origin", [None, True, "normal_return", "recovery_estimate"])
def test_charge_origin_must_be_validated_before_constructing_a_settlement(origin):
    with pytest.raises(ValueError, match="charge origin"):
        SettlementRequest(RunId("r"), Money.zero(), PricingBasis.ESTIMATE, "receipt", None, None, None,
                          origin=origin)


@pytest.mark.parametrize("gap", [None, 1, "x" * 2049])
def test_settlement_gap_rejects_malformed_or_unbounded_evidence(gap):
    with pytest.raises(ValueError, match="accounting gap"):
        SettlementRequest(RunId("r"), Money.zero(), PricingBasis.ESTIMATE, "provider:r", None, None, None, gap)


def test_settlement_gap_accepts_absence_and_boundary_text():
    for gap in ("", "x" * 2048):
        assert SettlementRequest(RunId("r"), Money.zero(), PricingBasis.ESTIMATE,
                                 "provider:r", None, None, None, gap).accounting_gap == gap


@pytest.mark.parametrize(
    ("amount", "micros"),
    (
        ("0", 0),
        ("0.0000004", 0),
        ("0.0000005", 1),
        ("0.0065895", 6_590),
        (1.25, 1_250_000),
    ),
)
def test_money_major_units_use_decimal_half_up(amount, micros):
    assert Money.from_major("USD", amount) == Money("USD", micros)


@pytest.mark.parametrize("amount", ("malformed", "NaN", "Infinity", "-0.01"))
def test_money_major_units_reject_ambiguous_values(amount):
    with pytest.raises(ValueError):
        Money.from_major("USD", amount)


def test_token_usage_is_disjoint_and_stderr_is_not_accounting_evidence():
    usage = TokenUsage(5, 7, 11, 13, UsageSource.STDERR)
    assert usage.total_tokens == 36
    assert not usage.trusted_for_accounting
    with pytest.raises(ValueError, match="negative"):
        TokenUsage(-1, 0)


def test_quote_and_scope_reject_naive_or_impossible_time():
    now = datetime.now(timezone.utc)
    quote = PriceQuote("m", "card", "v1", now, Money("USD", 10))
    assert quote.maximum_charge.micros == 10
    with pytest.raises(ValueError, match="timezone-aware"):
        PriceQuote("m", "card", "v1", now.replace(tzinfo=None), Money("USD", 10))
    with pytest.raises(ValueError, match="follow"):
        BudgetScope(
            "o/r", "daily", "USD", "day", now, now - timedelta(seconds=1),
            ZoneInfo("UTC"),
        )


@pytest.mark.parametrize(
    "instant,zone,hours,period_id",
    [
        (
            datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            "Australia/Sydney",
            23,
            "2026-10-04",
        ),
        (
            datetime(2026, 4, 5, 12, tzinfo=timezone.utc),
            "Australia/Sydney",
            25,
            "2026-04-05",
        ),
        (
            datetime(2028, 2, 29, 12, tzinfo=timezone.utc),
            "UTC",
            24,
            "2028-02-29",
        ),
    ],
)
def test_daily_budget_periods_follow_civil_dst_and_leap_boundaries(
    instant, zone, hours, period_id
):
    scope = daily_budget_scope(
        "o/r", "repository", "USD", instant, ZoneInfo(zone)
    )

    elapsed = (
        scope.period_end.astimezone(timezone.utc)
        - scope.period_start.astimezone(timezone.utc)
    )
    assert elapsed == timedelta(hours=hours)
    assert scope.period_id == period_id
