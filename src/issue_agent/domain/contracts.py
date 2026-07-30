"""Cross-cutting value objects for durable harness contracts.

These types deliberately reject ambiguous or lossy representations at the
boundary.  Operational adapters may translate provider-specific payloads into
them, but application and domain code never infer failure from an empty value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping, NewType
from zoneinfo import ZoneInfo

RunId = NewType("RunId", str)
DispatchId = NewType("DispatchId", str)
CommandId = NewType("CommandId", str)
ReservationId = NewType("ReservationId", str)

_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True, order=True)
class Money:
    """Integer micro-units in one ISO-style currency."""

    currency: str
    micros: int

    def __post_init__(self) -> None:
        if not _CURRENCY.fullmatch(self.currency):
            raise ValueError("currency must be a three-letter uppercase code")
        if self.micros < 0:
            raise ValueError("money cannot be negative")

    @classmethod
    def zero(cls, currency: str = "USD") -> Money:
        return cls(currency, 0)

    def __add__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise ValueError("cannot add different currencies")
        return Money(self.currency, self.micros + other.micros)

    def __sub__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise ValueError("cannot subtract different currencies")
        if other.micros > self.micros:
            raise ValueError("money subtraction cannot produce a negative value")
        return Money(self.currency, self.micros - other.micros)


class UsageSource(str, Enum):
    PROVIDER = "provider"
    ROLLOUT = "rollout"
    STDERR = "stderr"
    RECONCILED = "reconciled"


@dataclass(frozen=True)
class TokenUsage:
    """Disjoint token counters.

    ``input_tokens`` excludes cache reads and writes.  Adapters normalize a
    provider's cumulative convention exactly once before constructing this
    value.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    source: UsageSource = UsageSource.PROVIDER

    def __post_init__(self) -> None:
        if min(
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
        ) < 0:
            raise ValueError("token counters cannot be negative")

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def trusted_for_accounting(self) -> bool:
        return self.source is not UsageSource.STDERR


class PricingBasis(str, Enum):
    PROVIDER_REPORTED = "provider_reported"
    CONFIGURED_RATE_CARD = "configured_rate_card"
    LEARNED_ESTIMATE = "learned_estimate"
    RECONCILED = "reconciled"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True)
class PriceQuote:
    model: str
    rate_card_id: str
    rate_card_version: str
    effective_at: datetime
    maximum_charge: Money

    def __post_init__(self) -> None:
        _aware(self.effective_at, "effective_at")
        if not self.model.strip():
            raise ValueError("model is required")
        if not self.rate_card_id.strip() or not self.rate_card_version.strip():
            raise ValueError("rate-card identity is required")


@dataclass(frozen=True)
class BudgetScope:
    repository: str
    dimension: str
    currency: str
    period_id: str
    period_start: datetime
    period_end: datetime
    period_timezone: ZoneInfo

    def __post_init__(self) -> None:
        if not self.repository or not self.dimension or not self.period_id:
            raise ValueError("repository, dimension, and period_id are required")
        if not _CURRENCY.fullmatch(self.currency):
            raise ValueError("currency must be a three-letter uppercase code")
        _aware(self.period_start, "period_start")
        _aware(self.period_end, "period_end")
        if self.period_end <= self.period_start:
            raise ValueError("budget period end must follow its start")


class TestExecutionStatus(str, Enum):
    PASSED = "passed"
    TEST_FAILED = "test_failed"
    INFRA_ERROR = "infra_error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TestExecution:
    status: TestExecutionStatus
    command: tuple[str, ...]
    exit_code: int | None
    duration_ms: int
    stdout_digest: str
    stderr_digest: str
    executor_identity: str
    repository_revision: str

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("test command is required")
        if self.duration_ms < 0:
            raise ValueError("test duration cannot be negative")
        if not self.executor_identity or not self.repository_revision:
            raise ValueError("trusted executor and repository revision are required")
        if self.status is TestExecutionStatus.PASSED and self.exit_code != 0:
            raise ValueError("a passed test execution must have exit code zero")
        if self.status is TestExecutionStatus.TEST_FAILED and (
            self.exit_code is None or self.exit_code == 0
        ):
            raise ValueError("a test failure requires a non-zero exit code")
        if self.status in {
            TestExecutionStatus.TIMED_OUT,
            TestExecutionStatus.CANCELLED,
        } and self.exit_code is not None:
            raise ValueError("timeout/cancellation cannot claim an exit code")


class PolicyDisposition(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    INDETERMINATE = "indeterminate"


class EffectStatus(str, Enum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    UNKNOWN_OUTCOME = "unknown_outcome"


class CommandKind(str, Enum):
    RELABEL = "relabel"
    POST_COMMENT = "post_comment"
    CLOSE_ISSUE = "close_issue"
    ENABLE_AUTO_MERGE = "enable_auto_merge"
    UPDATE_BRANCH = "update_branch"
    RERUN_CHECKS = "rerun_checks"
    ADD_DEPENDENCY = "add_dependency"
    SUBMIT_REVIEW = "submit_review"
    PUBLISH_BRANCH = "publish_branch"
    OPEN_PULL_REQUEST = "open_pull_request"


@dataclass(frozen=True)
class LifecycleCommand:
    command_id: CommandId
    idempotency_key: str
    kind: CommandKind
    target: str
    expected_revision: str | None
    payload: Mapping[str, object] = field(default_factory=dict)
    config_version: str = ""
    repository: str = ""
    executor_identity: str = ""

    def __post_init__(self) -> None:
        if not str(self.command_id) or not self.idempotency_key or not self.target:
            raise ValueError("command identity, idempotency key, and target are required")
        if not self.repository or not self.executor_identity:
            raise ValueError(
                "command repository and executor identity are required"
            )
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class EffectReceipt:
    command_id: CommandId
    executor_identity: str
    started_at: datetime
    completed_at: datetime
    status: EffectStatus
    remote_object_id: str | None = None
    remote_revision: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not str(self.command_id) or not self.executor_identity:
            raise ValueError("command and executor identity are required")
        _aware(self.started_at, "started_at")
        _aware(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("effect completion cannot precede its start")
        successful = self.status is EffectStatus.SUCCEEDED
        if successful and self.error is not None:
            raise ValueError("a successful effect cannot carry an error")
        if not successful and not self.error:
            raise ValueError("an unsuccessful effect must carry an error")


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
