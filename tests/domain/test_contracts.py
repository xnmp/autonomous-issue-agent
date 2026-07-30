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
    TestExecution as ExecutionReceipt,
    TestExecutionStatus as ExecutionStatus,
    TokenUsage,
    UsageSource,
)


def test_money_is_integer_currency_safe_arithmetic():
    assert Money("USD", 7) + Money("USD", 5) == Money("USD", 12)
    with pytest.raises(ValueError, match="different currencies"):
        Money("USD", 7) + Money("AUD", 5)
    with pytest.raises(ValueError, match="negative"):
        Money("USD", -1)


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
