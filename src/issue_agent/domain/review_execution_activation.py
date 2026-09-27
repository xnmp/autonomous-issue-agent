"""Typed, untrusted legacy sources for review execution activation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json

from .resolve_activation import LegacyAbsent, LegacyFile, LegacyPresent, LegacyUnreadable, UnmappedLegacyEntry
from .workflow_identity import validate_workflow_integer, validate_workflow_text


def _files(*values: LegacyFile) -> None:
    if any(not isinstance(value, (LegacyAbsent, LegacyPresent, LegacyUnreadable)) for value in values):
        raise ValueError("legacy execution source requires typed file observations")


@dataclass(frozen=True)
class LegacyExecutionPRRecord:
    """Review session and round files for one PR in one state root."""

    pr: int
    session: LegacyFile
    round: LegacyFile
    root: str = "review"

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "legacy execution PR", minimum=1)
        _files(self.session, self.round)
        validate_workflow_text(self.root, "legacy state root label", limit=4096)


@dataclass(frozen=True)
class LegacyExecutionCounterRecord:
    """The signed legacy key retains issue and negated-PR namespaces separately."""

    key: int
    infra_deaths: LegacyFile
    root: str = "review"

    def __post_init__(self) -> None:
        if type(self.key) is not int or self.key == 0 or abs(self.key) > 2**63 - 1:
            raise ValueError("legacy execution counter key must be a nonzero signed target number")
        _files(self.infra_deaths)
        validate_workflow_text(self.root, "legacy state root label", limit=4096)


LegacyExecutionEntry = LegacyExecutionPRRecord | LegacyExecutionCounterRecord | UnmappedLegacyEntry


class LegacyReviewRunState(str, Enum):
    RESERVED = "reserved"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


MAX_LEGACY_SQL_TEXT_BYTES = 65536
MIN_SQL_ROWID = -(2**63)
MAX_SQL_ROWID = 2**63 - 1


def _rowid(value: object, name: str) -> None:
    if type(value) is not int or not MIN_SQL_ROWID <= value <= MAX_SQL_ROWID:
        raise ValueError(f"{name} must be a SQLite rowid")


@dataclass(frozen=True)
class LegacyExecutionRunRecord:
    """Uninterpreted owner run source; rowid is a page cursor, not identity."""

    source_rowid: int
    owner_id: str
    run_id: str
    dispatch_id: str
    kind: str
    state: LegacyReviewRunState
    config_version: str
    started_at: str | None
    completed_at: str | None
    outcome: str | None
    provider_receipt_id: str | None
    workflow_binding_json: str | None
    workflow_episode_key: str | None
    review_round_id: str | None
    review_child_id: str | None

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "legacy run cursor")
        if not isinstance(self.state, LegacyReviewRunState):
            raise ValueError("legacy run requires a typed state")
        for name in ("owner_id", "run_id", "dispatch_id", "kind", "config_version", "started_at",
                     "completed_at", "outcome", "provider_receipt_id", "workflow_binding_json",
                     "workflow_episode_key", "review_round_id", "review_child_id"):
            value = getattr(self, name)
            if value is None and name in {"started_at", "completed_at", "outcome", "provider_receipt_id",
                                          "workflow_binding_json", "workflow_episode_key", "review_round_id",
                                          "review_child_id"}:
                continue
            if not isinstance(value, str) or len(value.encode("utf-8", "surrogatepass")) > MAX_LEGACY_SQL_TEXT_BYTES:
                raise ValueError(f"legacy run {name} must be bounded text")
        if not self.owner_id or not self.run_id or not self.dispatch_id or not self.kind:
            raise ValueError("legacy run source lacks its identity")

    @property
    def source_digest(self) -> str:
        payload = {name: getattr(self, name) for name in (
            "owner_id", "run_id", "dispatch_id", "kind", "state", "config_version", "started_at",
            "completed_at", "outcome", "provider_receipt_id", "workflow_binding_json", "workflow_episode_key",
            "review_round_id", "review_child_id",
        )}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class UnreadableLegacyExecutionRun:
    source_rowid: int
    reason: str

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "unreadable run cursor")
        validate_workflow_text(self.reason, "unreadable run reason")


LegacyExecutionRunEntry = LegacyExecutionRunRecord | UnreadableLegacyExecutionRun


@dataclass(frozen=True)
class LegacyExecutionRunPage:
    entries: tuple[LegacyExecutionRunEntry, ...]
    next_rowid: int | None
    has_more: bool

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, (LegacyExecutionRunRecord, UnreadableLegacyExecutionRun)) for entry in self.entries
        ):
            raise ValueError("legacy run page requires typed entries")
        if self.next_rowid is not None:
            _rowid(self.next_rowid, "legacy run page cursor")
        if type(self.has_more) is not bool:
            raise ValueError("legacy run page continuation must be boolean")
        rowids = [entry.source_rowid for entry in self.entries]
        if rowids != sorted(set(rowids)) or (rowids and rowids[-1] != self.next_rowid):
            raise ValueError("legacy run page must advance through unique rowids")
        if self.has_more and not rowids:
            raise ValueError("legacy run page cannot continue without a cursor")
