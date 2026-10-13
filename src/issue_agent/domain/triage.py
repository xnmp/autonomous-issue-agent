"""The TRIAGE and STEWARD acts' results. Pure.

STEWARD writes testable acceptance criteria and flags a suspected duplicate;
TRIAGE grades the difficulty tier that routes the issue (owner, 2026-10-14:
one act each, on its own model). Before the split one TRIAGE result carried
all of it, and such legacy records still read back: `steward_from_record`
accepts any record with the steward's keys, so a combined TRIAGE leg satisfies
both acts.

Provenance: the agent's structured output is an untrusted claim. It becomes a
`Triage` or `Steward` only through `parse_triage` / `parse_steward`, which the
accounting runtime applies to the leg's output before recording it in the
leg's `result_json`. The one cross-reference, `duplicate_of`, is laundered
against the issue numbers the harness itself offered the leg: the schema the
harness writes enumerates them (read from GitHub at dispatch), so a number
outside that list is dropped, not trusted. Anything malformed or oversized
parses to None, which readers treat as no data: the issue runs at the default
tier, with no criteria, no duplicate stop and no comment line for it.

Records are flat objects of named fields, so a later act (tech-debt tracking,
a STEWARD extension) can add fields without disturbing these readers.
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
    """TRIAGE's result: the tier that routes the issue, and why."""

    tier: Tier
    rationale: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tier, Tier):
            raise ValueError("triage requires a tier")

    def record(self) -> dict[str, object]:
        return {"tier": self.tier.value, "rationale": self.rationale}


@dataclass(frozen=True)
class Steward:
    """STEWARD's result: what done means, and whether it was already asked for."""

    acceptance_criteria: tuple[str, ...] = ()
    duplicate_of: int | None = None
    duplicate_reason: str = ""

    def __post_init__(self) -> None:
        if self.duplicate_of is not None and (type(self.duplicate_of) is not int or self.duplicate_of <= 0):
            raise ValueError("duplicate_of is a positive issue number or None")

    def record(self) -> dict[str, object]:
        return {"acceptance_criteria": list(self.acceptance_criteria),
                "duplicate_of": self.duplicate_of, "duplicate_reason": self.duplicate_reason}


def _text(raw: object, limit: int) -> str | None:
    """Agent text a harness comment may embed: one line, bounded, and with
    hidden-comment openers neutralised so it can never carry a stop tag or the
    harness marker inside a harness-written comment. None when not text or
    over the bound (oversized output is malformed, not truncated). The
    neutralised form is no longer than the input, so a record re-parses under
    the same bounds."""
    if not isinstance(raw, str) or len(raw) > limit:
        return None
    return " ".join(raw.replace("<!--", "\u2039!--").split())


def parse_triage(raw: object) -> Triage | None:
    """The agent's output, or a recorded (already laundered) result read back
    from SQLite, as a Triage; None if malformed. Keys of the legacy combined
    result (criteria, duplicate) are ignored."""
    if not isinstance(raw, Mapping):
        return None
    tier = parse_tier(raw.get("tier"))
    rationale = _text(raw.get("rationale", ""), MAX_RATIONALE)
    return Triage(tier, rationale) if tier is not None and rationale is not None else None


def _steward(raw: object, allowed: Callable[[int], bool], criteria_wanted: bool) -> Steward | None:
    if not isinstance(raw, Mapping):
        return None
    # The list is required whenever the schema asked for it, and in every
    # record (which always carries it): that is what tells a steward result
    # from a tier-only triage record.
    criteria_raw = raw.get("acceptance_criteria", None if criteria_wanted else [])
    if not isinstance(criteria_raw, list) or len(criteria_raw) > MAX_CRITERIA:
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
    kept = tuple(c for c in criteria if c) if criteria_wanted else ()
    return Steward(kept, dup if valid_dup else None, reason if valid_dup else "")  # type: ignore[arg-type]


def parse_steward(raw: object, offered: Iterable[int], issue: int, criteria_wanted: bool = True) -> Steward | None:
    """The agent's output for issue `issue` as a Steward, or None if malformed.
    `duplicate_of` survives only if it is one of `offered` and not `issue`;
    criteria survive only if the harness asked for them (`criteria_wanted`)."""
    numbers = frozenset(n for n in offered if type(n) is int and n != issue)
    return _steward(raw, numbers.__contains__, criteria_wanted)


def steward_from_record(raw: object) -> Steward | None:
    """A recorded (already laundered) steward result. A legacy combined
    TRIAGE record has the same keys, so it reads as one; a tier-only record
    (post-split TRIAGE) lacks the criteria list and reads as None. Records
    always carry the list (`Steward.record`), even when empty."""
    return _steward(raw, lambda _n: True, True)


def triage_schema() -> dict:
    """TRIAGE's output contract."""
    return {
        "type": "object",
        "properties": {
            "tier": {"type": "string", "enum": [t.value for t in TIER_LADDER]},
            "rationale": {"type": "string"},
        },
        "required": ["tier", "rationale"],
        "additionalProperties": False,
    }


def steward_schema(offered: Iterable[int], criteria_wanted: bool = True) -> dict:
    """STEWARD's output contract. `duplicate_of` enumerates the issues the
    harness offered (the laundering list `offered_duplicates` reads back). An
    issue that states its own acceptance criteria gets none: its section stays
    the contract (`criteria_wanted` reads that back). The `acceptance_criteria`
    property is omitted then."""
    numbers = sorted({n for n in offered if type(n) is int and 0 < n <= _MAX_ISSUE})[:MAX_OFFERED]
    dup: dict = {"type": ["integer", "null"], "enum": [*numbers, None]} if numbers else {"type": "null"}
    return {
        "type": "object",
        "properties": {
            **({"acceptance_criteria": {"type": "array", "items": {"type": "string"}}} if criteria_wanted else {}),
            "duplicate_of": dup,
            "duplicate_reason": {"type": "string"},
        },
        "required": [*(["acceptance_criteria"] if criteria_wanted else []), "duplicate_of", "duplicate_reason"],
        "additionalProperties": False,
    }


def criteria_wanted(schema: object) -> bool:
    """Whether a `steward_schema` asked for acceptance criteria."""
    try:
        return "acceptance_criteria" in schema["properties"]  # type: ignore[index]
    except (KeyError, TypeError):
        return True


def offered_duplicates(schema: object) -> frozenset[int]:
    """The issue numbers a `steward_schema` offered; empty for anything else."""
    try:
        values = schema["properties"]["duplicate_of"].get("enum", ())  # type: ignore[index]
    except (KeyError, TypeError, AttributeError):
        return frozenset()
    if not isinstance(values, list):
        return frozenset()
    return frozenset(n for n in values if type(n) is int and n > 0)


__all__ = ["Steward", "Triage", "criteria_wanted", "offered_duplicates", "parse_steward", "parse_triage",
           "steward_from_record", "steward_schema", "triage_schema"]
