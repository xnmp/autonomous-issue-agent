"""Pure pricing and budget policy.

Rates are integer currency micros per million tokens.  Pricing rounds each
token class upward, so admission and in-flight caps never under-estimate by
discarding a fractional micro.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from types import MappingProxyType
from collections.abc import Callable
from typing import Mapping

from .contracts import Money, PriceQuote, TokenUsage

TOKENS_PER_MILLION = 1_000_000
DEFAULT_CACHE_READ_DISCOUNT = Decimal("0.1")


class UnknownModelPolicy(str, Enum):
    REJECT = "reject"
    USE_FALLBACK = "use_fallback"


@dataclass(frozen=True)
class ModelRates:
    """Price micros per million disjoint tokens."""

    input_micros: int
    output_micros: int
    cache_read_micros: int
    cache_write_micros: int

    def __post_init__(self) -> None:
        if min(
            self.input_micros,
            self.output_micros,
            self.cache_read_micros,
            self.cache_write_micros,
        ) <= 0:
            raise ValueError("every token class must have a positive rate")


@dataclass(frozen=True)
class RateTable:
    rate_card_id: str
    version: str
    currency: str
    rates: Mapping[str, ModelRates]
    unknown_model_policy: UnknownModelPolicy = UnknownModelPolicy.REJECT
    fallback: ModelRates | None = None

    def __post_init__(self) -> None:
        if not self.rate_card_id or not self.version:
            raise ValueError("rate-card identity is required")
        normalized = {
            name.strip().casefold(): value
            for name, value in self.rates.items()
            if name.strip()
        }
        if len(normalized) != len(self.rates):
            raise ValueError("model names must be non-empty and unique")
        if (
            self.unknown_model_policy is UnknownModelPolicy.USE_FALLBACK
            and self.fallback is None
        ):
            raise ValueError("fallback rates are required by unknown-model policy")
        object.__setattr__(self, "rates", MappingProxyType(normalized))
        Money(self.currency, 0)

    def rates_for(self, model: str) -> ModelRates:
        rates = self.rates.get(model.strip().casefold())
        if rates is not None:
            return rates
        if (
            self.unknown_model_policy is UnknownModelPolicy.USE_FALLBACK
            and self.fallback is not None
        ):
            return self.fallback
        raise KeyError(f"model {model!r} is not priceable by {self.rate_card_id}")

    def priceable(self, model: str) -> bool:
        try:
            self.rates_for(model)
        except KeyError:
            return False
        return True

    def gaps(self, models: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(model for model in models if not self.priceable(model))

    def quote(
        self, model: str, maximum_usage: TokenUsage, effective_at: datetime
    ) -> PriceQuote:
        return PriceQuote(
            model=model,
            rate_card_id=self.rate_card_id,
            rate_card_version=self.version,
            effective_at=effective_at,
            maximum_charge=price(maximum_usage, self.rates_for(model), self.currency),
        )


def price(usage: TokenUsage, rates: ModelRates, currency: str = "USD") -> Money:
    """Price disjoint usage with conservative per-class integer rounding."""

    products = (
        (usage.input_tokens, rates.input_micros),
        (usage.output_tokens, rates.output_micros),
        (usage.cache_read_tokens, rates.cache_read_micros),
        (usage.cache_write_tokens, rates.cache_write_micros),
    )
    micros = sum(
        (tokens * rate + TOKENS_PER_MILLION - 1) // TOKENS_PER_MILLION
        for tokens, rate in products
        if tokens
    )
    return Money(currency, micros)


def overrun(spent: Money, maximum: Money) -> Money | None:
    if spent.currency != maximum.currency:
        raise ValueError("cannot compare different currencies")
    return spent if spent.micros >= maximum.micros else None


@dataclass(frozen=True)
class BudgetSentinel:
    """Runtime-neutral sampling and overrun decision; adapters own killing."""

    sample_usage: Callable[[], TokenUsage | None]
    rates: ModelRates
    maximum: Money

    def sample(self) -> tuple[TokenUsage, Money] | None:
        usage = self.sample_usage()
        if usage is None or not usage.trusted_for_accounting:
            return None
        spent = price(usage, self.rates, self.maximum.currency)
        return (usage, spent) if overrun(spent, self.maximum) is not None else None


@dataclass(frozen=True)
class BudgetPolicy:
    limits: Mapping[tuple[str, str], Money]

    def __post_init__(self) -> None:
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))

    def maximum_for(self, role: str, leg_kind: str) -> Money:
        try:
            return self.limits[(role, leg_kind)]
        except KeyError as exc:
            raise KeyError(
                f"no budget policy for role={role!r}, leg={leg_kind!r}"
            ) from exc


def usd_per_million_to_micros(value: str | float | Decimal) -> int:
    """Convert a displayed USD/MTok rate to exact USD micros/MTok."""

    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid rate {value!r}") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("rate must be finite and positive")
    return int((amount * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_model_rate_table(
    raw: str,
) -> tuple[tuple[tuple[str, float, float], ...], tuple[tuple[str, float], ...], tuple[str, ...]]:
    """Parse the legacy ``model=IN/OUT[/CACHED_IN]`` syntax once.

    The first two return values preserve the existing configuration shape while
    callers migrate to ``RateTable``.  Errors are values so configuration can
    report every malformed entry in one pass.
    """

    table: list[tuple[str, float, float]] = []
    cached: list[tuple[str, float]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for raw_entry in raw.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        model, sep, rates_raw = entry.partition("=")
        model = model.strip().casefold()
        components = tuple(part.strip() for part in rates_raw.split("/"))
        if not sep or not model or len(components) not in (2, 3):
            errors.append(
                f"entry {entry!r} must look like 'model=IN/OUT[/CACHED_IN]' "
                "(USD per million tokens)"
            )
            continue
        try:
            rate_in = float(components[0])
            rate_out = float(components[1])
            rate_cached = (
                float(components[2])
                if len(components) == 3
                else float(Decimal(components[0]) * DEFAULT_CACHE_READ_DISCOUNT)
            )
            for value in (rate_in, rate_out, rate_cached):
                usd_per_million_to_micros(value)
        except (ValueError, InvalidOperation):
            errors.append(f"entry {entry!r} must have finite, positive rates")
            continue
        if model in seen:
            errors.append(
                f"entry {entry!r} names {model!r} more than once; keeping the first"
            )
            continue
        seen.add(model)
        table.append((model, rate_in, rate_out))
        cached.append((model, rate_cached))
    return tuple(table), tuple(cached), tuple(errors)
