"""Architecture pressure is repository history, never stored harness state (ADR 0017)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import TYPE_CHECKING

from .identity import login_eq

if TYPE_CHECKING:
    from .world import IssueView

LEDGER_PATH = "docs/architecture-pressure.md"
PASS_PREFIX = "Architecture pass:"
_OID = re.compile(r"[0-9a-f]{40,64}")


def _head(value: object) -> None:
    if not isinstance(value, str) or len(value) not in (40, 64) or not _OID.fullmatch(value):
        raise ValueError("repository history requires a commit oid")


@dataclass(frozen=True)
class LedgerAbsent:
    base_head: str

    def __post_init__(self) -> None:
        _head(self.base_head)


@dataclass(frozen=True)
class LedgerPresent:
    base_head: str
    ledger_head: str
    updated_at: datetime
    # An unavailable count still permits the independent age rule.
    merged_prs: int | None

    def __post_init__(self) -> None:
        _head(self.base_head)
        _head(self.ledger_head)
        if not isinstance(self.updated_at, datetime) or self.updated_at.utcoffset() is None:
            raise ValueError("ledger history requires an aware timestamp")
        if self.merged_prs is not None and (type(self.merged_prs) is not int or not 0 <= self.merged_prs <= 2**63 - 1):
            raise ValueError("merged PR count must be a bounded non-negative integer")


@dataclass(frozen=True)
class ArchitectureObservation:
    history: LedgerAbsent | LedgerPresent
    # Complete open-issue read, including unlabelled passes absent from READY pickup.
    issues: tuple[IssueView, ...] = ()


def architecture_pass(issue: IssueView, fix_bot: str, marker: str) -> bool:
    return (isinstance(issue.author, str) and login_eq(issue.author, fix_bot)
            and isinstance(issue.title, str) and len(issue.title) <= 256
            and issue.title.startswith(PASS_PREFIX) and isinstance(issue.body, str) and len(issue.body) <= 65536
            and marker in issue.body)


def architecture_owed(history: object, now: object, *, in_flight: bool = False) -> bool:
    """Unknown facts fail closed; an open pass, including a parked one, is in flight."""
    if in_flight or not isinstance(now, datetime) or now.utcoffset() is None:
        return False
    if isinstance(history, LedgerAbsent):
        return True
    if not isinstance(history, LedgerPresent) or history.updated_at > now:
        return False
    return (history.merged_prs is not None and history.merged_prs >= 5
            or history.base_head != history.ledger_head and now - history.updated_at > timedelta(hours=24))
