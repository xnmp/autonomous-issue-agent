from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from issue_agent.domain.contracts import (
    BudgetScope,
    CommandId,
    CommandKind,
    EffectReceipt,
    EffectStatus,
    LifecycleCommand,
    Money,
    PriceQuote,
    PricingBasis,
    ChargeOrigin,
    RunId,
    SettlementRequest,
    TestExecution as ExecutionReceipt,
    TestExecutionStatus as ExecutionStatus,
    TokenUsage,
    UsageSource,
    classify_test_exit_code,
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


@pytest.mark.parametrize(
    ("exit_code", "status"),
    (
        (0, ExecutionStatus.PASSED),
        (1, ExecutionStatus.TEST_FAILED),
        (2, ExecutionStatus.INFRA_ERROR),
        (124, ExecutionStatus.TIMED_OUT),
        (125, ExecutionStatus.INFRA_ERROR),
        (126, ExecutionStatus.INFRA_ERROR),
        (127, ExecutionStatus.INFRA_ERROR),
        (-9, ExecutionStatus.CANCELLED),
        (137, ExecutionStatus.CANCELLED),
        (None, ExecutionStatus.INFRA_ERROR),
    ),
)
def test_test_exit_classification_requires_confirmed_failure_code(
    exit_code, status
):
    assert classify_test_exit_code(exit_code) is status


def test_trusted_runner_can_declare_a_nonstandard_failure_exit_code() -> None:
    assert classify_test_exit_code(
        2, frozenset({1, 2})
    ) is ExecutionStatus.TEST_FAILED
    assert classify_test_exit_code(
        127, frozenset({127})
    ) is ExecutionStatus.INFRA_ERROR


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


def test_only_genuine_nonzero_test_exit_can_be_red_evidence():
    trusted = dict(
        command=("pytest",),
        duration_ms=1,
        stdout_digest="a",
        stderr_digest="b",
        executor_identity="verifier",
        repository_revision="deadbeef",
    )
    result = ExecutionReceipt(
        ExecutionStatus.TEST_FAILED, exit_code=1, **trusted
    )
    assert result.status is ExecutionStatus.TEST_FAILED
    with pytest.raises(ValueError, match="non-zero"):
        ExecutionReceipt(ExecutionStatus.TEST_FAILED, exit_code=0, **trusted)
    with pytest.raises(ValueError, match="cannot claim"):
        ExecutionReceipt(ExecutionStatus.TIMED_OUT, exit_code=124, **trusted)


def test_lifecycle_payload_is_immutable_and_receipt_state_is_total():
    command = LifecycleCommand(
        CommandId("cmd-1"),
        "comment:issue:1:digest",
        CommandKind.POST_COMMENT,
        "issue/1",
        None,
        {"body": "hello"},
        repository="o/r",
        executor_identity="o/r:fix",
    )
    with pytest.raises(TypeError):
        command.payload["body"] = "changed"  # type: ignore[index]

    now = datetime.now(timezone.utc)
    receipt = EffectReceipt(
        command.command_id,
        "broker",
        now,
        now,
        EffectStatus.SUCCEEDED,
        remote_object_id="comment-1",
    )
    assert receipt.error is None
    with pytest.raises(ValueError, match="must carry an error"):
        EffectReceipt(
            command.command_id,
            "broker",
            now,
            now,
            EffectStatus.UNKNOWN_OUTCOME,
        )
