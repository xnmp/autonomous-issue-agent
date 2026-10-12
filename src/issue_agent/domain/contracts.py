"""Cross-cutting value objects for durable harness contracts.

These types deliberately reject ambiguous or lossy representations at the
boundary.  Operational adapters may translate provider-specific payloads into
them, but application and domain code never infer failure from an empty value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from types import MappingProxyType
from typing import Mapping, NewType
from zoneinfo import ZoneInfo

from .repository import canonical_repository
from .workflow_identity import WorkflowBinding, WorkflowFamily, validate_workflow_identifier, validate_workflow_integer

RunId = NewType("RunId", str)
DispatchId = NewType("DispatchId", str)
CommandId = NewType("CommandId", str)
ReservationId = NewType("ReservationId", str)


@dataclass(frozen=True)
class WorkflowAttemptIdentity:
    binding: WorkflowBinding
    run_id: RunId
    leg_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WorkflowBinding):
            raise ValueError("workflow attempt requires a validated binding")
        for name, value in (("run_id", self.run_id), ("leg_id", self.leg_id)):
            validate_workflow_identifier(value, f"workflow attempt {name}")


@dataclass(frozen=True)
class ContinuationAdmission:
    attempt: WorkflowAttemptIdentity
    admission_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, WorkflowAttemptIdentity) or self.attempt.binding.authority.family not in (
            WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR,
        ):
            raise ValueError("continuation admission requires a selected fixed component attempt")
        validate_workflow_integer(self.admission_version, "continuation admission version", minimum=1)


_CURRENCY = re.compile(r"^[A-Z]{3}$")
MAX_RECEIPT_ERROR_BYTES = 8 * 1024


def _bounded_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


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

    @classmethod
    def from_major(
        cls,
        currency: str,
        amount: str | int | float | Decimal,
    ) -> Money:
        """Convert currency major units with one repository-wide rounding rule."""

        try:
            decimal = Decimal(str(amount))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("money amount must be numeric") from exc
        if not decimal.is_finite() or decimal < 0:
            raise ValueError("money amount must be finite and non-negative")
        micros = int(
            (decimal * Decimal(1_000_000)).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
        return cls(currency, micros)

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
    ESTIMATE = "estimate"
    LEARNED_ESTIMATE = "learned_estimate"
    RECONCILED = "reconciled"
    SYNTHETIC = "synthetic"


class ChargeOrigin(str, Enum):
    RECOVERY_ESTIMATE = "recovery_estimate"
    NORMAL_RETURN = "normal_return"


@dataclass(frozen=True)
class SettlementRequest:
    run_id: RunId
    amount: Money
    basis: PricingBasis
    provider_receipt_id: str
    usage: TokenUsage | None
    rate_card_id: str | None
    rate_card_version: str | None
    accounting_gap: str = ""
    origin: ChargeOrigin = ChargeOrigin.NORMAL_RETURN

    def __post_init__(self) -> None:
        if not str(self.run_id) or not self.provider_receipt_id:
            raise ValueError("run and provider receipt identities are required")
        if not isinstance(self.accounting_gap, str) or len(self.accounting_gap) > 2048:
            raise ValueError("accounting gap must be bounded text")
        if not isinstance(self.origin, ChargeOrigin):
            raise ValueError("charge origin must be typed")
        if (
            self.basis is PricingBasis.CONFIGURED_RATE_CARD
            and (not self.rate_card_id or not self.rate_card_version)
        ):
            raise ValueError("configured charges require rate-card provenance")


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
        object.__setattr__(
            self, "repository", canonical_repository(self.repository)
        )
        if not self.repository or not self.dimension or not self.period_id:
            raise ValueError("repository, dimension, and period_id are required")
        if not _CURRENCY.fullmatch(self.currency):
            raise ValueError("currency must be a three-letter uppercase code")
        _aware(self.period_start, "period_start")
        _aware(self.period_end, "period_end")
        if self.period_end <= self.period_start:
            raise ValueError("budget period end must follow its start")


def daily_budget_scope(
    repository: str,
    dimension: str,
    currency: str,
    at: datetime,
    period_timezone: ZoneInfo,
) -> BudgetScope:
    """Build a civil-day scope without assuming every day has 24 hours."""

    _aware(at, "at")
    local_date = at.astimezone(period_timezone).date()
    start = datetime.combine(local_date, time.min, period_timezone)
    end = datetime.combine(
        local_date + timedelta(days=1), time.min, period_timezone
    )
    return BudgetScope(
        repository,
        dimension,
        currency,
        local_date.isoformat(),
        start,
        end,
        period_timezone,
    )


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
    CANCEL_CHECKS = "cancel_checks"
    SUBMIT_REVIEW = "submit_review"
    DISMISS_REVIEW = "dismiss_review"
    PUBLISH_BRANCH = "publish_branch"
    OPEN_PULL_REQUEST = "open_pull_request"
    UPDATE_PULL_REQUEST_BODY = "update_pull_request_body"
    ENSURE_LABEL = "ensure_label"
    SET_ISSUE_LABELS = "set_issue_labels"
    SET_REPOSITORY_AUTO_MERGE = "set_repository_auto_merge"
    DELETE_RELAY_HOOK = "delete_relay_hook"
    START_RELAY_FORWARD = "start_relay_forward"


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
    workflow: WorkflowBinding | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "repository", canonical_repository(self.repository)
        )
        if not str(self.command_id) or not self.idempotency_key or not self.target:
            raise ValueError("command identity, idempotency key, and target are required")
        if not self.repository or not self.executor_identity:
            raise ValueError(
                "command repository and executor identity are required"
            )
        if self.workflow is not None and (
            not isinstance(self.workflow, WorkflowBinding)
            or self.workflow.fence.key.repository != self.repository
        ):
            raise ValueError("command workflow binding must match its repository")
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
        if self.error is not None:
            object.__setattr__(
                self,
                "error",
                _bounded_utf8(self.error, MAX_RECEIPT_ERROR_BYTES),
            )


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
