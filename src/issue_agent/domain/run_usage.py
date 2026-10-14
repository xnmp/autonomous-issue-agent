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
        if any(s.reported_cost is not None for s in self.samples):
            raise ValueError("measured usage is priced from its tokens; a fixed charge is an estimate")
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


def reserved_maximum_usage(
    session_id: str, model: str, maximum: Money, source: UsageSource, detail: str,
) -> EstimatedRunUsage:
    """A leg whose spend cannot be priced settles at its reservation's
    maximum: the real spend is unknown but bounded by it, so the caps never
    see less."""
    sample = ModelUsage(session_id or "unknown-session", model.strip() or "<runtime-default>",
                        TokenUsage(0, 0, source=source), reported_cost=maximum)
    return EstimatedRunUsage(detail, (sample,))


# A session's cumulative per-model counters as one leg left them, kept with
# the leg so the session's next resumed leg can price its own delta (#389).
_SNAPSHOT_MODELS = 64
_SNAPSHOT_COUNTER = 10**12


def snapshot_record(session_id: str, samples: tuple[ModelUsage, ...]) -> dict[str, object]:
    return {"session_id": session_id, "models": {s.model: list(_counters(s.usage)) for s in samples}}


def parse_snapshot(session_id: str, raw: object) -> tuple[ModelUsage, ...] | None:
    """A snapshot the session recorded, or None when absent, another
    session's, or malformed."""
    if not isinstance(raw, dict) or raw.get("session_id") != session_id:
        return None
    models = raw.get("models")
    if not isinstance(models, dict) or not 0 < len(models) <= _SNAPSHOT_MODELS:
        return None
    try:
        samples = tuple(ModelUsage(session_id, name, TokenUsage(*counters)) for name, counters in models.items()
                        if isinstance(counters, list) and len(counters) == 4
                        and all(type(c) is int and 0 <= c <= _SNAPSHOT_COUNTER for c in counters))
        _validate_samples(samples)
    except (ValueError, TypeError):
        return None
    return samples if len(samples) == len(models) else None


def usage_since(current: tuple[ModelUsage, ...], previous: tuple[ModelUsage, ...],
                floor: tuple[ModelUsage, ...] = ()) -> tuple[ModelUsage, ...] | None:
    """Per-model usage between two cumulative snapshots of one session, or
    None when the counters were reset: one went backwards, a model vanished,
    or the delta is below `floor`, usage the leg is known to have spent."""
    before = {s.model.strip().casefold(): s.usage for s in previous}
    if not set(before) <= {s.model.strip().casefold() for s in current}:
        return None
    delta: dict[str, ModelUsage] = {}
    for sample in current:
        prior = _counters(before.get(sample.model.strip().casefold(), TokenUsage(0, 0)))
        counters = tuple(a - b for a, b in zip(_counters(sample.usage), prior))
        if min(counters) < 0:
            return None
        delta[sample.model.strip().casefold()] = ModelUsage(
            sample.session_id, sample.model, TokenUsage(*counters, source=sample.usage.source))
    for known in floor:
        spent = delta.get(known.model.strip().casefold())
        least = _counters(known.usage)
        if any(least) and (spent is None or any(a < b for a, b in zip(_counters(spent.usage), least))):
            return None
    return tuple(s for s in delta.values() if s.usage.total_tokens)


def _counters(usage: TokenUsage) -> tuple[int, int, int, int]:
    return usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens


def bounded_by_rates(observed: RunUsage, table: RateTable | None, maximum: Money) -> RunUsage:
    """Tokens a model with no rate spent settle at the reserved maximum: a
    fallback price is never trusted (#389)."""
    unrated = sorted({s.model for s in observed.samples if table is not None and s.reported_cost is None
                      and s.usage.total_tokens and not table.priceable(s.model)})
    if not unrated:
        return observed
    first = observed.samples[0]
    return reserved_maximum_usage(first.session_id, first.model, maximum, first.usage.source,
                                  f"no harness rate for {', '.join(unrated)[:400]}; charged the reserved maximum")


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
