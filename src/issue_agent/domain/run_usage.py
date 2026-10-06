"""Observed usage within one reservation, including native runtime children."""

from dataclasses import dataclass

from .contracts import Money, TokenUsage, UsageSource
from .pricing import RateTable, price


@dataclass(frozen=True)
class ModelUsage:
    session_id: str
    model: str
    usage: TokenUsage

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


def _validate_samples(samples: tuple[ModelUsage, ...]) -> None:
    if (not isinstance(samples, tuple) or len(samples) > 65
            or any(not isinstance(s, ModelUsage) for s in samples)
            or len({(s.session_id, s.model.strip().casefold()) for s in samples}) != len(samples)):
        raise ValueError("run usage requires unique bounded model samples")


@dataclass(frozen=True)
class MeasuredRunUsage:
    samples: tuple[ModelUsage, ...]

    def __post_init__(self) -> None:
        _validate_samples(self.samples)
        if not self.samples:
            raise ValueError("measured run usage cannot infer a free run from no evidence")


@dataclass(frozen=True)
class UnavailableRunUsage:
    detail: str
    samples: tuple[ModelUsage, ...] = ()

    def __post_init__(self) -> None:
        _validate_samples(self.samples)
        if not isinstance(self.detail, str) or not self.detail or len(self.detail) > 2048:
            raise ValueError("unavailable usage requires a bounded reason")


RunUsage = MeasuredRunUsage | UnavailableRunUsage


def total_usage(samples: tuple[ModelUsage, ...]) -> TokenUsage:
    _validate_samples(samples)
    return TokenUsage(
        sum(s.usage.input_tokens for s in samples),
        sum(s.usage.output_tokens for s in samples),
        sum(s.usage.cache_read_tokens for s in samples),
        sum(s.usage.cache_write_tokens for s in samples),
        samples[0].usage.source if samples else TokenUsage(0, 0).source,
    )


def price_run_usage(observed: MeasuredRunUsage, table: RateTable) -> Money:
    if not isinstance(observed, MeasuredRunUsage):
        raise ValueError("unavailable usage cannot settle a reservation")
    return Money(table.currency, sum(
        price(s.usage, table.rates_for(s.model), table.currency).micros
        for s in observed.samples
    ))
