"""Test fixtures + builders for the domain behavioural suite.

Makes `src/` importable and provides terse constructors so each test reads as a
constructed WorldState → asserted Actions, with no I/O.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from issue_agent.domain.labels import Label  # noqa: E402
from issue_agent.domain.world import (  # noqa: E402
    Check,
    Comment,
    IssueView,
    PRView,
    Review,
    WorldState,
)


def labels(*names: str) -> frozenset[Label]:
    return frozenset(Label(n) for n in names)


def issue(number: int, *label_names: str, **kw) -> IssueView:
    kw.setdefault("title", f"Issue {number}")
    kw["labels"] = labels(*label_names)
    return IssueView(number=number, **kw)


def pr(number: int, *label_names: str, **kw) -> PRView:
    kw["labels"] = labels(*label_names)
    kw.setdefault("state", "OPEN")
    # Default to a harness-shaped branch that resolves to an issue, so the fixer's
    # "no linked issue → escalate" guard doesn't fire in tests that don't care
    # about linkage. Override head_ref_name (or set closing_issue) to test the
    # issueless path explicitly.
    kw.setdefault("head_ref_name", f"fix/{number}-x")
    # Default author to the fixer bot used across domain tests ("fix-bot"), so the
    # fixer's "only drive my own PRs" gate treats a builder PR as ours unless a test
    # overrides author= to exercise the human-PR path.
    kw.setdefault("author", "fix-bot")
    return PRView(number=number, **kw)


def world(issues=(), prs=()) -> WorldState:
    return WorldState(issues=tuple(issues), prs=tuple(prs))


__all__ = [
    "labels",
    "issue",
    "pr",
    "world",
    "Check",
    "Comment",
    "Review",
    "Label",
]
