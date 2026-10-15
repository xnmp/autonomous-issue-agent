"""Harness gaps the harness files about itself, into its own repository, as
`policy.base_ci_actions` files a failing base workflow into the target: a fix
leg's `harness_issue` report (a defect in what the harness gives it, distinct
from `needs`, its question for a human), and an act whose machine ends reach
`backoff.STALL_STREAK` on a live item. Judgment stops are never filed. Pure.

No agent text is published (owner, 2026-10-15): the report is an untrusted
claim `parse_harness_gap` bounds, kept on the harness host (leg receipt,
journal). The issue holds harness facts only, a non-public target unnamed,
and only a human's `ai-ready` queues it (ADR 0001).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

MAX_GAP_TITLE = 150
MAX_GAP_BODY = 8000
# The harness's gap reports per UTC day, filings and recurrence comments
# together, across every target: dedup by key is the main bound, this one
# keeps a bug from spamming the harness repository. Enforced in act.
GAP_REPORTS_PER_DAY = 10


@dataclass(frozen=True)
class HarnessGap:
    """An agent-reported harness defect, bounded; never published."""

    title: str
    body: str

    def __post_init__(self) -> None:
        if (not isinstance(self.title, str) or not self.title or len(self.title) > MAX_GAP_TITLE
                or "\n" in self.title or not isinstance(self.body, str) or len(self.body) > MAX_GAP_BODY):
            raise ValueError("a harness gap is a bounded one-line title and a bounded body")

    def record(self) -> dict[str, str]:
        return {"title": self.title, "body": self.body}


def parse_harness_gap(raw: object) -> HarnessGap | None:
    """The agent's `harness_issue`, or its recorded form, as a HarnessGap;
    None when absent or malformed. Oversized text is malformed, not truncated
    (as `triage._text`)."""
    if not isinstance(raw, Mapping):
        return None
    title, body = raw.get("title"), raw.get("body", "")
    if not isinstance(title, str) or not isinstance(body, str) or len(title) > MAX_GAP_TITLE or len(body) > MAX_GAP_BODY:
        return None
    title = " ".join(title.split())
    return HarnessGap(title, body.strip()) if title else None


@dataclass(frozen=True)
class GapTarget:
    """The target as a public gap issue may name it: by name, linked, when
    public; else only as "a private target" (no name, digest or link: a
    digest of a guessable name is confirmable offline). The repository stays
    in the local dedup key, so private targets may share one public issue."""

    repo: str
    public: bool

    @property
    def label(self) -> str:
        return self.repo if self.public else "a private target"

    def link(self, number: int) -> str:
        """A link to the item, only for a public target ("" otherwise)."""
        return f"https://github.com/{self.repo}/issues/{number}" if self.public else ""


__all__ = ["GAP_REPORTS_PER_DAY", "GapTarget", "HarnessGap", "MAX_GAP_BODY", "MAX_GAP_TITLE", "parse_harness_gap"]
