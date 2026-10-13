"""The TRIAGE act's result: a difficulty tier, testable acceptance criteria and
a suspected duplicate. Pure.

Provenance: the agent's structured output is an untrusted claim. It becomes a
`Triage` only through `parse_triage`, which the accounting runtime applies to
the leg's output before recording it in the leg's `result_json`. The one
cross-reference, `duplicate_of`, is laundered against the issue numbers the
harness itself offered the leg: the schema the harness writes enumerates them
(read from GitHub at dispatch), so a number outside that list is dropped, not
trusted. Anything malformed or oversized parses to None, which readers treat
as no triage data: the issue runs at the default tier, with no duplicate stop
and no comment.

The record is a flat object of named fields, so a later act (tech-debt
tracking) can add fields without disturbing these readers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from .routing import TIER_LADDER, Tier, parse_tier

MAX_CRITERIA = 20
MAX_CRITERION = 500
MAX_REASON = 500
MAX_RATIONALE = 2000
MAX_OFFERED = 300
_MAX_ISSUE = 2_147_483_647


@dataclass(frozen=True)
class Triage:
    tier: Tier
    acceptance_criteria: tuple[str, ...] = ()
    duplicate_of: int | None = None
    duplicate_reason: str = ""
    rationale: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tier, Tier):
            raise ValueError("triage requires a tier")
        if self.duplicate_of is not None and (type(self.duplicate_of) is not int or self.duplicate_of <= 0):
            raise ValueError("duplicate_of is a positive issue number or None")

    def record(self) -> dict[str, object]:
        return {"tier": self.tier.value, "acceptance_criteria": list(self.acceptance_criteria),
                "duplicate_of": self.duplicate_of, "duplicate_reason": self.duplicate_reason,
                "rationale": self.rationale}


def _text(raw: object, limit: int) -> str | None:
    """Agent text a harness comment may embed: one line, bounded, and with
    hidden-comment openers neutralised so it can never carry a stop tag or the
    harness marker inside a harness-written comment. None when not text or
    over the bound (oversized output is malformed, not truncated)."""
    if not isinstance(raw, str) or len(raw) > limit:
        return None
    return " ".join(raw.replace("<!--", "<​!--").split())


def _parse(raw: object, allowed: Callable[[int], bool]) -> Triage | None:
    if not isinstance(raw, Mapping):
        return None
    tier = parse_tier(raw.get("tier"))
    criteria_raw = raw.get("acceptance_criteria", [])
    rationale = _text(raw.get("rationale", ""), MAX_RATIONALE)
    if tier is None or rationale is None or not isinstance(criteria_raw, list) or len(criteria_raw) > MAX_CRITERIA:
        return None
    criteria = tuple(_text(item, MAX_CRITERION) for item in criteria_raw)
    if any(item is None for item in criteria):
        return None
    reason = _text(raw.get("duplicate_reason") or "", MAX_REASON)
    if reason is None:
        return None
    dup = raw.get("duplicate_of")
    # A suspected duplicate needs a known target and a stated reason; failing
    # either drops only the claim, toward no stop.
    valid_dup = type(dup) is int and 0 < dup <= _MAX_ISSUE and allowed(dup) and bool(reason)
    return Triage(tier, tuple(c for c in criteria if c), dup if valid_dup else None,  # type: ignore[arg-type]
                  reason if valid_dup else "", rationale)


def parse_triage(raw: object, offered: Iterable[int], issue: int) -> Triage | None:
    """The agent's output for issue `issue` as a Triage, or None if malformed.
    `duplicate_of` survives only if it is one of `offered` and not `issue`."""
    numbers = frozenset(n for n in offered if type(n) is int and n != issue)
    return _parse(raw, numbers.__contains__)


def triage_from_record(raw: object) -> Triage | None:
    """A recorded (already laundered) triage read back from SQLite."""
    return _parse(raw, lambda _n: True)


def triage_schema(offered: Iterable[int]) -> dict:
    """The output contract. `duplicate_of` enumerates the issues the harness
    offered (the laundering list `offered_duplicates` reads back)."""
    numbers = sorted({n for n in offered if type(n) is int and 0 < n <= _MAX_ISSUE})[:MAX_OFFERED]
    dup: dict = {"type": ["integer", "null"], "enum": [*numbers, None]} if numbers else {"type": "null"}
    return {
        "type": "object",
        "properties": {
            "tier": {"type": "string", "enum": [t.value for t in TIER_LADDER]},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "duplicate_of": dup,
            "duplicate_reason": {"type": "string"},
            "rationale": {"type": "string"},
        },
        "required": ["tier", "acceptance_criteria", "duplicate_of", "duplicate_reason", "rationale"],
        "additionalProperties": False,
    }


def offered_duplicates(schema: object) -> frozenset[int]:
    """The issue numbers a `triage_schema` offered; empty for anything else."""
    try:
        values = schema["properties"]["duplicate_of"].get("enum", ())  # type: ignore[index]
    except (KeyError, TypeError, AttributeError):
        return frozenset()
    if not isinstance(values, list):
        return frozenset()
    return frozenset(n for n in values if type(n) is int and n > 0)


__all__ = ["Triage", "offered_duplicates", "parse_triage", "triage_from_record", "triage_schema"]
