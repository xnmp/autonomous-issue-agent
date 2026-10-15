"""The TRIAGE and STEWARD acts' results. Pure.

STEWARD writes testable acceptance criteria, flags a suspected duplicate,
proposes a genuine dependency ("blocked by");
TRIAGE grades the difficulty tier that routes the issue (owner, 2026-10-14:
one act each, on its own model).

Provenance: the agent's structured output is an untrusted claim. It becomes a
`Triage` or `Steward` only through `parse_triage` / `parse_steward`, which the
accounting runtime applies to the leg's output before recording it in the
leg's `result_json`. The one cross-reference, `duplicate_of`, is laundered
against the issue numbers the harness itself offered the leg: the schema the
harness writes enumerates them (read from GitHub at dispatch), so a number
outside that list is dropped, not trusted. `blocked_by` is laundered the same
way against the open issues offered, and `link_refusal` rechecks it on fresh
reads before the harness writes the link. Anything malformed or oversized
parses to None, which readers treat as no data: the issue runs at the default
tier, with no criteria, no duplicate stop and no comment line for it.

Records are flat objects of named fields, so a later act (tech-debt tracking,
a STEWARD extension) can add fields without disturbing these readers.
"""

from __future__ import annotations

import re
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
    # An open issue this one cannot be correctly done before (never mere overlap).
    blocked_by: int | None = None
    blocked_by_reason: str = ""

    def __post_init__(self) -> None:
        for name in ("duplicate_of", "blocked_by"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} is a positive issue number or None")
        if (self.blocked_by is None) != (not self.blocked_by_reason):
            raise ValueError("a blocker is carried exactly with its reason")

    def record(self) -> dict[str, object]:
        return {"acceptance_criteria": list(self.acceptance_criteria),
                "duplicate_of": self.duplicate_of, "duplicate_reason": self.duplicate_reason,
                "blocked_by": self.blocked_by, "blocked_by_reason": self.blocked_by_reason}


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
    from SQLite, as a Triage; None if malformed."""
    if not isinstance(raw, Mapping):
        return None
    tier = parse_tier(raw.get("tier"))
    rationale = _text(raw.get("rationale", ""), MAX_RATIONALE)
    return Triage(tier, rationale) if tier is not None and rationale is not None else None


def _issue_claim(raw: Mapping, key: str, why_key: str, allowed: Callable[[int], bool]) -> tuple[int | None, str] | None:
    """A cross-reference claim (`duplicate_of`, `blocked_by`) with its one-line
    reason: kept only with a known target and a bounded, stated reason. A reason
    that is not text makes the whole result malformed (None); an unknown target
    or an oversized reason (a model explaining why there is none) drops only the claim."""
    raw_reason = raw.get(why_key) or ""
    if not isinstance(raw_reason, str):
        return None
    reason = _text(raw_reason, MAX_REASON) or ""
    target = raw.get(key)
    if isinstance(target, int) and not isinstance(target, bool) and 0 < target <= _MAX_ISSUE and allowed(target) and reason:
        return target, reason
    return None, ""


def _steward(
    raw: object, allowed: Callable[[int], bool], criteria_wanted: bool, blockable: Callable[[int], bool],
) -> Steward | None:
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
    # A claim needs a known target and a stated reason; failing either drops
    # only the claim, toward no stop and no link.
    dup = _issue_claim(raw, "duplicate_of", "duplicate_reason", allowed)
    block = _issue_claim(raw, "blocked_by", "blocked_by_reason", blockable)
    if dup is None or block is None:
        return None
    kept = tuple(c for c in criteria if c) if criteria_wanted else ()
    return Steward(kept, dup[0], dup[1], block[0], block[1])


def parse_steward(
    raw: object, offered: Iterable[int], issue: int, criteria_wanted: bool = True, blockable: Iterable[int] = (),
) -> Steward | None:
    """The agent's output for issue `issue` as a Steward, or None if malformed.
    `duplicate_of` survives only if it is one of `offered`, `blocked_by` only if
    one of `blockable` (the open issues offered), and neither if it is `issue`;
    criteria survive only if the harness asked for them (`criteria_wanted`)."""
    numbers = frozenset(n for n in offered if type(n) is int and n != issue)
    blockers = frozenset(n for n in blockable if type(n) is int and n != issue)
    return _steward(raw, numbers.__contains__, criteria_wanted, blockers.__contains__)


def steward_from_record(raw: object, issue: int | None = None) -> Steward | None:
    """A recorded (already laundered) steward result for `issue`. Records always carry the
    list (`Steward.record`), even when empty. A claim naming the issue itself (a corrupt record: the write-time parse forbids
    it) is dropped, never acted on."""
    def other(n: int) -> bool:
        return n != issue
    return _steward(raw, other, True, other)


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


def _enum(numbers: Iterable[int]) -> dict:
    listed = sorted({n for n in numbers if type(n) is int and 0 < n <= _MAX_ISSUE})[:MAX_OFFERED]
    return {"type": ["integer", "null"], "enum": [*listed, None]} if listed else {"type": "null"}


def steward_schema(offered: Iterable[int], criteria_wanted: bool = True, blockable: Iterable[int] = ()) -> dict:
    """STEWARD's output contract. `duplicate_of` enumerates the issues the
    harness offered and `blocked_by` the open ones among them (the laundering
    lists `offered_duplicates` / `offered_blockers` read back). An
    issue that states its own acceptance criteria gets none: its section stays
    the contract (`criteria_wanted` reads that back). The `acceptance_criteria`
    property is omitted then."""
    return {
        "type": "object",
        "properties": {
            **({"acceptance_criteria": {"type": "array", "items": {"type": "string"}}} if criteria_wanted else {}),
            "duplicate_of": _enum(offered),
            "duplicate_reason": {"type": "string"},
            "blocked_by": _enum(blockable),
            "blocked_by_reason": {"type": "string"},
        },
        "required": [*(["acceptance_criteria"] if criteria_wanted else []), "duplicate_of", "duplicate_reason",
                     "blocked_by", "blocked_by_reason"],
        "additionalProperties": False,
    }


# An ATX heading (at most three leading spaces) titled "acceptance criteria".
# An issue template's commented-out heading is not the author's criteria.
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_AC_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+acceptance criteria\b", re.IGNORECASE | re.MULTILINE)


def states_criteria(issue_body: str) -> bool:
    """Whether an issue states its own acceptance criteria, so the steward is
    not asked for any. A heading inside a code block also counts: the cost is
    a steward that writes none, and the reviewer then judges against the issue."""
    return _AC_HEADING.search(_HTML_COMMENT.sub("", issue_body)) is not None


def criteria_wanted(schema: object) -> bool:
    """Whether a `steward_schema` asked for acceptance criteria."""
    try:
        return "acceptance_criteria" in schema["properties"]  # type: ignore[index]
    except (KeyError, TypeError):
        return True


def _offered(schema: object, key: str) -> frozenset[int]:
    try:
        values = schema["properties"][key].get("enum", ())  # type: ignore[index]
    except (KeyError, TypeError, AttributeError):
        return frozenset()
    if not isinstance(values, list):
        return frozenset()
    return frozenset(n for n in values if type(n) is int and n > 0)


def offered_duplicates(schema: object) -> frozenset[int]:
    """The issue numbers a `steward_schema` offered; empty for anything else."""
    return _offered(schema, "duplicate_of")


def offered_blockers(schema: object) -> frozenset[int]:
    """The open issue numbers a `steward_schema` offered as blockers; empty for anything else."""
    return _offered(schema, "blocked_by")


def link_refusal(issue: int, blocker: int, blocker_open: bool, blocker_blocked_by: Iterable[int]) -> str:
    """Why the harness must not write "#`issue` blocked by #`blocker`", judged on
    fresh reads at write time ("" when it may): the claim was laundered at
    dispatch, but the blocker may have closed since, and a blocker that is
    itself blocked by this issue would make a cycle neither can leave."""
    if blocker == issue:
        return "an issue cannot block itself"
    if not blocker_open:
        return f"#{blocker} is closed"
    if issue in blocker_blocked_by:
        return f"#{blocker} is itself blocked by this issue, so linking would make a cycle"
    return ""


__all__ = ["Steward", "Triage", "criteria_wanted", "link_refusal", "offered_blockers",
           "offered_duplicates", "parse_steward", "parse_triage", "steward_from_record",
           "steward_schema", "triage_schema"]
