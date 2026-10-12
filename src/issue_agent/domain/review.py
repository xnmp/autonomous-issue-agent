"""A review leg's verdict: the agent's claim, parsed and bounded. Pure.

Refactor slice 5c deleted the review family (rounds, rejection ceilings,
re-arms, multi and delegated review). What is left is the one fact the review
role needs from a leg's output: an APPROVE or REQUEST_CHANGES and its body.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

REVIEW_BODY_LIMIT = 60000
_VERDICT_EVENTS = ("APPROVE", "REQUEST_CHANGES")


@dataclass(frozen=True)
class Verdict:
    """A parsed review verdict, ready to submit as one GitHub review."""

    event: Literal["APPROVE", "REQUEST_CHANGES"]
    body: str

    def __post_init__(self) -> None:
        if self.event not in _VERDICT_EVENTS or not isinstance(self.body, str) or len(self.body) > REVIEW_BODY_LIMIT:
            raise ValueError("a verdict is APPROVE or REQUEST_CHANGES with a bounded body")
        if self.event == "REQUEST_CHANGES" and not self.body.strip():
            raise ValueError("a change request needs a body")


def parse_verdict(structured: object) -> Verdict | None:
    """The agent's verdict claim, or None when it is not a usable one. The
    agent's output is never authority: anything but an exact APPROVE or
    REQUEST_CHANGES with a text body (non-empty for a change request) is no
    verdict, and the body is bounded to what one GitHub review holds."""
    if not isinstance(structured, Mapping):
        return None
    event, body = structured.get("decision"), structured.get("body")
    if event not in _VERDICT_EVENTS or not isinstance(body, str):
        return None
    body = body.strip()[:REVIEW_BODY_LIMIT].strip()
    if event == "REQUEST_CHANGES" and not body:
        return None
    return Verdict(event, body)
