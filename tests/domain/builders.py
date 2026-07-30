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
    ClosingRef,
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
    kw.setdefault("merge_state", "CLEAN")
    return PRView(number=number, **kw)


def closing_ref(pr_number: int, issue_number: int, author: str = "human",
                base_ref_name: str = "main") -> ClosingRef:
    """An OPEN PR GitHub says closes `issue_number` (#39). Author defaults to a
    stranger, since the guard's subject is PRs the harness did not open — pass
    the fix bot's login explicitly to exercise the in-flight (not-a-duplicate)
    case."""
    return ClosingRef(pr=pr_number, issue=issue_number, author=author,
                      base_ref_name=base_ref_name)


def world(issues=(), prs=(), now: str = "", closing_refs=()) -> WorldState:
    # `now` defaults to "" (unstamped): every time-based gate reads that as
    # "elapsed time unknown" and stays inert, so tests that don't care about
    # time are unaffected by anything the clock does.
    # `closing_refs` defaults to empty = NOT COLLECTED, which the duplicate-PR
    # guard reads as "no evidence of a duplicate" (fail-open) — so every test
    # written before #39 keeps dispatching exactly as it did.
    return WorldState(issues=tuple(issues), prs=tuple(prs),
                      closing_refs=tuple(closing_refs), now=now)


__all__ = [
    "labels",
    "issue",
    "pr",
    "closing_ref",
    "world",
    "Check",
    "Comment",
    "Review",
    "Label",
]
