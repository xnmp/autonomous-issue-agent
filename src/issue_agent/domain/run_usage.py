"""Observed usage within one reservation, including native runtime children."""

from dataclasses import dataclass

from .contracts import Money, TokenUsage, UsageSource
from .pricing import RateTable, price


@dataclass(frozen=True)
class ModelUsage:
    session_id: str
    model: str
    usage: TokenUsage
    reported_cost: Money | None = None
    request_id: str = ""

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v.strip() or len(v) > 256
               for v in (self.session_id, self.model)):
            raise ValueError("usage requires bounded session and model identities")
        if not isinstance(self.usage, TokenUsage) or not self.usage.trusted_for_accounting:
            raise ValueError("model usage requires trusted token counters")
        if (not isinstance(self.usage.source, UsageSource)
                or any(type(v) is not int or v < 0 for v in (
                    self.usage.input_tokens, self.usage.output_tokens,
                    self.usage.cache_read_tokens, self.usage.cache_write_tokens))):
            raise ValueError("model usage requires integer counters and typed provenance")
        if self.reported_cost is not None and not isinstance(self.reported_cost, Money):
            raise ValueError("reported request cost requires typed money")
        if not isinstance(self.request_id, str) or len(self.request_id) > 256:
            raise ValueError("request identity must be bounded text")


def _validate_samples(samples: tuple[ModelUsage, ...]) -> None:
    if (not isinstance(samples, tuple) or len(samples) > 4096 + 4 * 65
            or any(not isinstance(s, ModelUsage) for s in samples)
            or len({(s.session_id, bool(s.request_id), s.request_id or s.model.strip().casefold())
                    for s in samples}) != len(samples)):
        raise ValueError("run usage requires unique bounded request or model samples")


@dataclass(frozen=True)
class MeasuredRunUsage:
    samples: tuple[ModelUsage, ...]
    rates: RateTable | None = None

    def __post_init__(self) -> None:
        _validate_samples(self.samples)
        if not self.samples:
            raise ValueError("measured run usage cannot infer a free run from no evidence")
        if self.rates is not None and not isinstance(self.rates, RateTable):
            raise ValueError("run pricing requires a frozen rate table")


@dataclass(frozen=True)
class EstimatedRunUsage:
    detail: str
    samples: tuple[ModelUsage, ...] = ()
    rates: RateTable | None = None

    def __post_init__(self) -> None:
        _validate_samples(self.samples)
        if not isinstance(self.detail, str) or not self.detail or len(self.detail) > 2048:
            raise ValueError("estimated usage requires a bounded gap")
        if self.rates is not None and not isinstance(self.rates, RateTable):
            raise ValueError("run pricing requires a frozen rate table")


RunUsage = MeasuredRunUsage | EstimatedRunUsage


def total_usage(samples: tuple[ModelUsage, ...]) -> TokenUsage:
    _validate_samples(samples)
    return TokenUsage(
        sum(s.usage.input_tokens for s in samples),
        sum(s.usage.output_tokens for s in samples),
        sum(s.usage.cache_read_tokens for s in samples),
        sum(s.usage.cache_write_tokens for s in samples),
        samples[0].usage.source if samples else TokenUsage(0, 0).source,
    )


def price_run_usage(observed: RunUsage, table: RateTable | None) -> Money:
    table = observed.rates or table
    currency = table.currency if table is not None else "USD"
    if any(s.reported_cost is not None and s.reported_cost.currency != currency for s in observed.samples):
        raise ValueError("reported request cost currency differs from the rate card")
    reported = tuple(s.reported_cost for s in observed.samples if s.reported_cost is not None)
    priced = () if table is None else tuple(
        price(s.usage, table.rates_for(s.model), currency)
        for s in observed.samples if s.reported_cost is None and table.priceable(s.model)
    )
    return Money(currency, sum(m.micros for m in (*reported, *priced)))


def usage_lower_bound(first: TokenUsage, second: TokenUsage) -> TokenUsage:
    """Cumulative counters and deduplicated receipts overlap; neither erases the other."""
    return TokenUsage(
        max(first.input_tokens, second.input_tokens),
        max(first.output_tokens, second.output_tokens),
        max(first.cache_read_tokens, second.cache_read_tokens),
        max(first.cache_write_tokens, second.cache_write_tokens),
        UsageSource.ROLLOUT,
    )
