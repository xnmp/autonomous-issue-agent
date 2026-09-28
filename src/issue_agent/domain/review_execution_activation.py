"""Typed, untrusted legacy sources for review execution activation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json

from .resolve_activation import LegacyAbsent, LegacyFile, LegacyPresent, LegacyUnreadable, UnmappedLegacyEntry
from .review_workflow import ReviewedBaseline
from .review_workflow_policy import ReviewSubmissionReceipt
from .review_execution import ReviewRoundIdentity, ReviewRoundOutcome
from .review_rounds import ReviewRoundFacts, FinalizedReviewRound, DeferredReviewPreparation, finalize_review_round
from .workflow import ObservedTransition
from .workflow_identity import (
    PullRequestRef, WorkflowRevision, canonical_workflow_repository, validate_workflow_identifier,
    validate_workflow_integer, validate_workflow_text,
)


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

    @property
    def source_bytes(self) -> int:
        return sum(len(value.encode("utf-8", "surrogatepass")) for value in (
            self.owner_id, self.run_id, self.dispatch_id, self.kind, self.config_version,
            self.started_at, self.completed_at, self.outcome, self.provider_receipt_id,
            self.workflow_binding_json, self.workflow_episode_key, self.review_round_id, self.review_child_id,
        ) if value is not None)


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


MAX_LEGACY_RUN_INVENTORY_ROWS = 8192
MAX_LEGACY_RUN_INVENTORY_BYTES = 2**24


@dataclass(frozen=True)
class CompleteLegacyReviewRuns:
    """Adapter scan output; activation must revalidate caller-held values in SQL."""

    lineage: LegacyReviewOwnerLineage
    runs: tuple[LegacyExecutionRunRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.lineage, LegacyReviewOwnerLineage) or not isinstance(self.runs, tuple):
            raise ValueError("complete review runs require typed lineage and sources")
        if (len(self.runs) > MAX_LEGACY_RUN_INVENTORY_ROWS
                or any(not isinstance(run, LegacyExecutionRunRecord) or run.owner_id not in self.lineage.owners
                       for run in self.runs)
                or len({run.run_id for run in self.runs}) != len(self.runs)
                or sum(run.source_bytes for run in self.runs) > MAX_LEGACY_RUN_INVENTORY_BYTES):
            raise ValueError("complete review runs require bounded unique owner sources")

    @property
    def source_digest(self) -> str:
        sources = tuple(sorted((run.run_id, run.source_digest) for run in self.runs))
        return hashlib.sha256(json.dumps((self.lineage.source_digest, sources),
                                         separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class BlockedLegacyReviewRuns:
    lineage: LegacyReviewOwnerLineage
    reason: str
    owner_id: str | None = None
    source_rowid: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.lineage, LegacyReviewOwnerLineage):
            raise ValueError("blocked review runs require typed lineage")
        validate_workflow_text(self.reason, "blocked review run reason")
        if self.owner_id is not None and self.owner_id not in self.lineage.owners:
            raise ValueError("blocked review run owner is outside the lineage")
        if self.source_rowid is not None:
            _rowid(self.source_rowid, "blocked review run cursor")


LEGACY_SUBMISSION_COMMAND_COLUMNS = (
    "command_id", "idempotency_key", "logical_key", "occurrence", "kind", "target", "expected_revision",
    "config_version", "repository", "executor_identity", "payload_json", "payload_digest", "state", "attempts",
    "created_at", "updated_at", "refusals", "workflow_binding_json", "workflow_reconciliation_json",
    "attempt_owner", "prewrite_read_json",
)
LEGACY_SUBMISSION_RECEIPT_COLUMNS = (
    "command_id", "executor_identity", "started_at", "completed_at", "status", "remote_object_id",
    "remote_revision", "error", "receipt_digest",
)
_SUBMISSION_INTEGERS = frozenset({"occurrence", "attempts", "refusals"})
_SUBMISSION_OPTIONAL_TEXT = frozenset({
    "expected_revision", "workflow_binding_json", "workflow_reconciliation_json", "attempt_owner",
    "prewrite_read_json", "remote_object_id", "remote_revision", "error",
})


def _submission_fields(
    fields: tuple[tuple[str, str | int | None], ...], names: tuple[str, ...],
) -> None:
    if not isinstance(fields, tuple) or tuple(name for name, _ in fields) != names:
        raise ValueError("submission source has the wrong fields or order")
    for name, value in fields:
        if name in _SUBMISSION_INTEGERS:
            if type(value) is not int or value < 0 or value > MAX_SQL_ROWID:
                raise ValueError(f"submission {name} must be a nonnegative SQL integer")
        elif value is None:
            if name not in _SUBMISSION_OPTIONAL_TEXT:
                raise ValueError(f"submission {name} cannot be absent")
        elif not isinstance(value, str) or len(value.encode("utf-8", "surrogatepass")) > MAX_LEGACY_SQL_TEXT_BYTES:
            raise ValueError(f"submission {name} must be bounded text")


@dataclass(frozen=True)
class LegacyReviewSubmissionSource:
    """Exact raw command and optional effect receipt for an offline review import."""

    source_rowid: int
    command_fields: tuple[tuple[str, str | int | None], ...]
    receipt_fields: tuple[tuple[str, str | int | None], ...] | None

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "submission source cursor")
        _submission_fields(self.command_fields, LEGACY_SUBMISSION_COMMAND_COLUMNS)
        command = dict(self.command_fields)
        if (command["kind"] != "submit_review" or not isinstance(command["command_id"], str)
                or not command["command_id"] or not isinstance(command["repository"], str)
                or canonical_workflow_repository(command["repository"]) != command["repository"]):
            raise ValueError("submission source requires a canonical review command")
        if self.receipt_fields is not None:
            _submission_fields(self.receipt_fields, LEGACY_SUBMISSION_RECEIPT_COLUMNS)
            if dict(self.receipt_fields)["command_id"] != command["command_id"]:
                raise ValueError("submission receipt belongs to another command")

    @property
    def source_digest(self) -> str:
        payload = {"command": self.command_fields, "receipt": self.receipt_fields}
        return hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class UnreadableLegacyReviewSubmission:
    source_rowid: int
    reason: str

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "unreadable submission cursor")
        validate_workflow_text(self.reason, "unreadable submission reason")


LegacyReviewSubmissionEntry = LegacyReviewSubmissionSource | UnreadableLegacyReviewSubmission


@dataclass(frozen=True)
class LegacyReviewSubmissionPage:
    entries: tuple[LegacyReviewSubmissionEntry, ...]
    next_rowid: int | None
    has_more: bool

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, (LegacyReviewSubmissionSource, UnreadableLegacyReviewSubmission))
            for entry in self.entries
        ):
            raise ValueError("submission page requires typed source entries")
        if self.next_rowid is not None:
            _rowid(self.next_rowid, "submission page cursor")
        if type(self.has_more) is not bool:
            raise ValueError("submission page continuation must be boolean")
        rowids = [entry.source_rowid for entry in self.entries]
        if rowids != sorted(set(rowids)) or (rowids and rowids[-1] != self.next_rowid):
            raise ValueError("submission page must advance through unique rowids")
        if self.has_more and not rowids:
            raise ValueError("submission page cannot continue without a cursor")


LEGACY_REVIEW_SUBMISSION_RECEIPT_NAMESPACES = frozenset({
    "review-round-submission", "review-round-submission-index", "review-recorded-submission",
})
LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS = (
    "namespace", "scope", "key", "claimed_at", "reusable_after", "payload_json",
)


@dataclass(frozen=True)
class LegacyReviewSubmissionReceiptSource:
    """Raw registration, round index or recorded-review receipt from dedupe_keys."""

    source_rowid: int
    fields: tuple[tuple[str, str | None], ...]

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "submission receipt cursor")
        if (not isinstance(self.fields, tuple)
                or any(not isinstance(field, tuple) or len(field) != 2 for field in self.fields)
                or tuple(name for name, _ in self.fields) != LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS):
            raise ValueError("submission receipt requires exact fields and order")
        values = dict(self.fields)
        for name, value in self.fields:
            if value is None and name in {"reusable_after", "payload_json"}:
                continue
            if not isinstance(value, str) or len(value.encode("utf-8", "surrogatepass")) > MAX_LEGACY_SQL_TEXT_BYTES:
                raise ValueError(f"submission receipt {name} must be bounded text")
        if (values["namespace"] not in LEGACY_REVIEW_SUBMISSION_RECEIPT_NAMESPACES
                or not values["key"] or canonical_workflow_repository(values["scope"]) != values["scope"]):
            raise ValueError("submission receipt requires a canonical review source")

    @property
    def source_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.fields, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class UnreadableLegacyReviewSubmissionReceipt:
    source_rowid: int
    reason: str

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "unreadable submission receipt cursor")
        validate_workflow_text(self.reason, "unreadable submission receipt reason")


LegacyReviewSubmissionReceiptEntry = LegacyReviewSubmissionReceiptSource | UnreadableLegacyReviewSubmissionReceipt


@dataclass(frozen=True)
class LegacyReviewSubmissionReceiptPage:
    entries: tuple[LegacyReviewSubmissionReceiptEntry, ...]
    next_rowid: int | None
    has_more: bool

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, (LegacyReviewSubmissionReceiptSource, UnreadableLegacyReviewSubmissionReceipt))
            for entry in self.entries
        ):
            raise ValueError("submission receipt page requires typed entries")
        if self.next_rowid is not None:
            _rowid(self.next_rowid, "submission receipt page cursor")
        if type(self.has_more) is not bool:
            raise ValueError("submission receipt continuation must be boolean")
        rowids = [entry.source_rowid for entry in self.entries]
        if rowids != sorted(set(rowids)) or (rowids and rowids[-1] != self.next_rowid):
            raise ValueError("submission receipt page must advance through unique rowids")
        if self.has_more and not rowids:
            raise ValueError("submission receipt page cannot continue without a cursor")


LEGACY_REVIEW_ROUND_COLUMNS = (
    "round_id", "repository", "target_kind", "target_number", "execution_epoch",
    "execution_episode", "generation", "sequence", "claim_occurrence_id",
    "plan_json", "planning_json", "closure_json", "outcome_json",
)
_ROUND_INTEGER_COLUMNS = frozenset({"target_number", "execution_epoch", "generation", "sequence"})
_ROUND_JSON_COLUMNS = frozenset({"plan_json", "planning_json", "closure_json", "outcome_json"})
MAX_LEGACY_ROUND_JSON_BYTES = 2**21
MAX_LEGACY_ROUND_PAGE_BYTES = 2**24


@dataclass(frozen=True)
class LegacyReviewRoundSource:
    """Uninterpreted, bounded SQL round row; cursor is not source identity."""

    source_rowid: int
    fields: tuple[tuple[str, str | int | None], ...]

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "review round cursor")
        if (not isinstance(self.fields, tuple)
                or any(not isinstance(field, tuple) or len(field) != 2 for field in self.fields)
                or tuple(name for name, _ in self.fields) != LEGACY_REVIEW_ROUND_COLUMNS):
            raise ValueError("review round source requires exact fields and order")
        for name, value in self.fields:
            if name in _ROUND_INTEGER_COLUMNS:
                if type(value) is not int or value < 0 or value > MAX_SQL_ROWID:
                    raise ValueError(f"review round {name} must be a bounded integer")
            elif value is None:
                if name not in {"closure_json", "outcome_json"}:
                    raise ValueError(f"review round {name} is absent")
            elif (not isinstance(value, str)
                  or len(value.encode("utf-8", "surrogatepass")) > (
                      MAX_LEGACY_ROUND_JSON_BYTES if name in _ROUND_JSON_COLUMNS else MAX_LEGACY_SQL_TEXT_BYTES
                  )):
                raise ValueError(f"review round {name} must be bounded text")
        values = dict(self.fields)
        if (not values["round_id"] or canonical_workflow_repository(values["repository"]) != values["repository"]
                or values["target_kind"] != "pr" or values["target_number"] < 1
                or values["execution_epoch"] < 1 or values["sequence"] < 1):
            raise ValueError("review round source lacks its canonical SQL identity")

    @property
    def source_bytes(self) -> int:
        return sum(len(value.encode("utf-8", "surrogatepass")) if isinstance(value, str) else 8
                   for _, value in self.fields if value is not None)

    @property
    def source_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.fields, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class UnreadableLegacyReviewRound:
    source_rowid: int
    reason: str

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "unreadable review round cursor")
        validate_workflow_text(self.reason, "unreadable review round reason")


LegacyReviewRoundEntry = LegacyReviewRoundSource | UnreadableLegacyReviewRound


@dataclass(frozen=True)
class LegacyReviewRoundPage:
    entries: tuple[LegacyReviewRoundEntry, ...]
    next_rowid: int | None
    has_more: bool

    def __post_init__(self) -> None:
        if (not isinstance(self.entries, tuple)
                or any(not isinstance(entry, (LegacyReviewRoundSource, UnreadableLegacyReviewRound))
                       for entry in self.entries)):
            raise ValueError("review round page requires typed entries")
        if self.next_rowid is not None:
            _rowid(self.next_rowid, "review round page cursor")
        if type(self.has_more) is not bool:
            raise ValueError("review round page continuation must be boolean")
        rowids = [entry.source_rowid for entry in self.entries]
        if rowids != sorted(set(rowids)) or (rowids and rowids[-1] != self.next_rowid):
            raise ValueError("review round page must advance through unique rowids")
        if self.has_more and not rowids:
            raise ValueError("review round page cannot continue without a cursor")


MAX_LEGACY_ROUND_INVENTORY_ROWS = 65536
MAX_LEGACY_ROUND_INVENTORY_BYTES = 2**26


@dataclass(frozen=True)
class CompleteLegacyReviewRounds:
    """A bounded whole-repository observation; activation must recheck SQL."""

    repository: str
    rounds: tuple[LegacyReviewRoundSource, ...]

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository or not isinstance(self.rounds, tuple):
            raise ValueError("complete review rounds require a canonical repository and tuple")
        if (len(self.rounds) > MAX_LEGACY_ROUND_INVENTORY_ROWS
                or any(not isinstance(source, LegacyReviewRoundSource)
                       or dict(source.fields)["repository"] != self.repository for source in self.rounds)
                or sum(source.source_bytes for source in self.rounds) > MAX_LEGACY_ROUND_INVENTORY_BYTES):
            raise ValueError("complete review rounds require bounded readable repository sources")
        identities = [dict(source.fields) for source in self.rounds]
        if (len({row["round_id"] for row in identities}) != len(identities)
                or len({(row["target_number"], row["execution_epoch"], row["execution_episode"], row["sequence"])
                        for row in identities}) != len(identities)):
            raise ValueError("complete review rounds require unique round and sequence identities")

    @property
    def source_digest(self) -> str:
        parts = tuple(sorted((dict(source.fields)["round_id"], source.source_digest) for source in self.rounds))
        return hashlib.sha256(json.dumps((self.repository, parts), separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class BlockedLegacyReviewRounds:
    repository: str
    reason: str
    source_rowid: int | None = None

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("blocked review rounds require a canonical repository")
        validate_workflow_text(self.reason, "blocked review round reason")
        if self.source_rowid is not None:
            _rowid(self.source_rowid, "blocked review round cursor")


LEGACY_REVIEW_OUTCOME_RECEIPT_NAMESPACES = frozenset({
    "review-round-outcome", "review-round-preparation", "review-round-stale-productivity",
    "review-round-superseded-productivity", "review-round-ceiling-productivity",
})
MAX_LEGACY_OUTCOME_RECEIPT_JSON_BYTES = 2**21


@dataclass(frozen=True)
class LegacyReviewOutcomeReceiptSource:
    """Uninterpreted outcome or productivity receipt from the durable source log."""

    source_rowid: int
    fields: tuple[tuple[str, str | None], ...]

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "review outcome receipt cursor")
        if (not isinstance(self.fields, tuple)
                or any(not isinstance(field, tuple) or len(field) != 2 for field in self.fields)
                or tuple(name for name, _ in self.fields) != LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS):
            raise ValueError("review outcome receipt requires exact fields and order")
        for name, value in self.fields:
            if value is None and name == "reusable_after":
                continue
            if (not isinstance(value, str)
                    or len(value.encode("utf-8", "surrogatepass")) > (
                        MAX_LEGACY_OUTCOME_RECEIPT_JSON_BYTES if name == "payload_json" else MAX_LEGACY_SQL_TEXT_BYTES
                    )):
                raise ValueError(f"review outcome receipt {name} must be bounded text")
        values = dict(self.fields)
        if (values["namespace"] not in LEGACY_REVIEW_OUTCOME_RECEIPT_NAMESPACES
                or canonical_workflow_repository(values["scope"]) != values["scope"]
                or not values["key"] or not values["claimed_at"] or not values["payload_json"]):
            raise ValueError("review outcome receipt lacks its canonical source identity")

    @property
    def source_bytes(self) -> int:
        return sum(len(value.encode("utf-8", "surrogatepass")) for _, value in self.fields if value is not None)

    @property
    def source_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.fields, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class UnreadableLegacyReviewOutcomeReceipt:
    source_rowid: int
    reason: str

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "unreadable review outcome receipt cursor")
        validate_workflow_text(self.reason, "unreadable review outcome receipt reason")


LegacyReviewOutcomeReceiptEntry = LegacyReviewOutcomeReceiptSource | UnreadableLegacyReviewOutcomeReceipt


@dataclass(frozen=True)
class LegacyReviewOutcomeReceiptPage:
    entries: tuple[LegacyReviewOutcomeReceiptEntry, ...]
    next_rowid: int | None
    has_more: bool

    def __post_init__(self) -> None:
        if (not isinstance(self.entries, tuple)
                or any(not isinstance(entry, (LegacyReviewOutcomeReceiptSource, UnreadableLegacyReviewOutcomeReceipt))
                       for entry in self.entries)):
            raise ValueError("review outcome receipt page requires typed entries")
        if self.next_rowid is not None:
            _rowid(self.next_rowid, "review outcome receipt page cursor")
        if type(self.has_more) is not bool:
            raise ValueError("review outcome receipt page continuation must be boolean")
        rowids = [entry.source_rowid for entry in self.entries]
        if rowids != sorted(set(rowids)) or (rowids and rowids[-1] != self.next_rowid):
            raise ValueError("review outcome receipt page must advance through unique rowids")
        if self.has_more and not rowids:
            raise ValueError("review outcome receipt page cannot continue without a cursor")


@dataclass(frozen=True)
class BlockedLegacyReviewOutcomeReceiptPage:
    repository: str
    reason: str
    source_rowid: int | None = None

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("blocked outcome receipt page requires a canonical repository")
        validate_workflow_text(self.reason, "blocked outcome receipt page reason")
        if self.source_rowid is not None:
            _rowid(self.source_rowid, "blocked outcome receipt scan cursor")


MAX_LEGACY_OUTCOME_RECEIPT_INVENTORY_ROWS = 65536
MAX_LEGACY_OUTCOME_RECEIPT_INVENTORY_BYTES = 2**26


@dataclass(frozen=True)
class CompleteLegacyReviewOutcomeReceipts:
    """A bounded whole-repository observation; activation must recheck SQL."""

    repository: str
    receipts: tuple[LegacyReviewOutcomeReceiptSource, ...]

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository or not isinstance(self.receipts, tuple):
            raise ValueError("complete outcome receipts require a canonical repository and tuple")
        if (len(self.receipts) > MAX_LEGACY_OUTCOME_RECEIPT_INVENTORY_ROWS
                or any(not isinstance(source, LegacyReviewOutcomeReceiptSource)
                       or dict(source.fields)["scope"] != self.repository for source in self.receipts)
                or sum(source.source_bytes for source in self.receipts) > MAX_LEGACY_OUTCOME_RECEIPT_INVENTORY_BYTES):
            raise ValueError("complete outcome receipts require bounded readable repository sources")
        identities = [(dict(source.fields)["namespace"], dict(source.fields)["key"]) for source in self.receipts]
        if len(set(identities)) != len(identities):
            raise ValueError("complete outcome receipts require unique namespace and key identities")

    @property
    def source_digest(self) -> str:
        parts = tuple(sorted((dict(source.fields)["namespace"], dict(source.fields)["key"], source.source_digest)
                             for source in self.receipts))
        return hashlib.sha256(json.dumps((self.repository, parts), separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class BlockedLegacyReviewOutcomeReceipts:
    repository: str
    reason: str
    source_rowid: int | None = None

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("blocked outcome receipts require a canonical repository")
        validate_workflow_text(self.reason, "blocked outcome receipt reason")
        if self.source_rowid is not None:
            _rowid(self.source_rowid, "blocked outcome receipt cursor")


@dataclass(frozen=True)
class VerifiedLegacyRoundOutcomeStorage:
    """One typed round outcome is paired with its exact durable receipt."""

    round_source: LegacyReviewRoundSource
    receipt_source: LegacyReviewOutcomeReceiptSource
    facts: ReviewRoundFacts
    outcome: ReviewRoundOutcome

    def __post_init__(self) -> None:
        if (not isinstance(self.round_source, LegacyReviewRoundSource)
                or not isinstance(self.receipt_source, LegacyReviewOutcomeReceiptSource)
                or not isinstance(self.facts, ReviewRoundFacts)
                or not isinstance(self.outcome, ReviewRoundOutcome)):
            raise ValueError("round outcome storage requires typed sources")
        row = dict(self.round_source.fields)
        receipt = dict(self.receipt_source.fields)
        identity = self.facts.plan.round
        key = identity.binding.rejection.fence.key
        if (not isinstance(key.target, PullRequestRef)
                or self.outcome.round != identity or row["round_id"] != identity.round_id
                or row["repository"] != key.repository
                or row["target_number"] != key.target.number
                or row["execution_epoch"] != identity.binding.execution_epoch
                or row["execution_episode"] != identity.binding.execution_episode
                or row["generation"] != identity.binding.rejection.fence.generation
                or row["sequence"] != identity.sequence
                or receipt["namespace"] != "review-round-outcome"
                or receipt["scope"] != key.repository or receipt["key"] != identity.round_id
                or receipt["reusable_after"] is not None):
            raise ValueError("round outcome storage has contradictory identities")
        derived = finalize_review_round(self.facts)
        if not isinstance(derived, (FinalizedReviewRound, DeferredReviewPreparation)) or derived.outcome != self.outcome:
            raise ValueError("round outcome disagrees with its closure facts")

    @property
    def source_digest(self) -> str:
        parts = (self.round_source.source_digest, self.receipt_source.source_digest)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class UnverifiedLegacyRoundOutcomeStorage:
    round_id: str
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.round_id, "unverified round outcome")
        validate_workflow_text(self.reason, "unverified round outcome reason")


MAX_LEGACY_REVIEW_OWNERS = 64


@dataclass(frozen=True)
class LegacyReviewOwnerLink:
    """One retained deployment retirement; rowid is observation provenance."""

    source_rowid: int
    owner_id: str
    successor_owner_id: str
    repository: str
    role: str
    retired_at: str

    def __post_init__(self) -> None:
        _rowid(self.source_rowid, "retired owner cursor")
        for name in ("owner_id", "successor_owner_id", "repository", "role", "retired_at"):
            validate_workflow_text(getattr(self, name), f"retired owner {name}", limit=MAX_LEGACY_SQL_TEXT_BYTES)

    @property
    def source_digest(self) -> str:
        fields = (self.owner_id, self.successor_owner_id, self.repository, self.role, self.retired_at)
        return hashlib.sha256(json.dumps(fields, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class LegacyReviewOwnerLineage:
    """Complete bounded owner closure from current, implicit, configured and retired sources."""

    repository: str
    current_owner: str
    configured_prior_owners: tuple[str, ...]
    owners: tuple[str, ...]
    links: tuple[LegacyReviewOwnerLink, ...]

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("owner lineage requires a canonical repository")
        validate_workflow_identifier(self.current_owner, "current review owner")
        implicit_owner = f"{self.repository}:review"
        if self.current_owner != implicit_owner and not self.current_owner.startswith(implicit_owner + ":"):
            raise ValueError("current review owner does not match its repository and role")
        if (not isinstance(self.configured_prior_owners, tuple)
                or not isinstance(self.owners, tuple) or not isinstance(self.links, tuple)
                or len(self.owners) > MAX_LEGACY_REVIEW_OWNERS
                or any(not isinstance(link, LegacyReviewOwnerLink) for link in self.links)):
            raise ValueError("owner lineage requires bounded typed sources")
        for owner in (*self.configured_prior_owners, *self.owners):
            validate_workflow_identifier(owner, "review owner")
        if (len(set(self.configured_prior_owners)) != len(self.configured_prior_owners)
                or self.current_owner in self.configured_prior_owners
                or implicit_owner in self.configured_prior_owners
                or len(set(self.owners)) != len(self.owners)
                or self.owners[0:1] != (self.current_owner,)):
            raise ValueError("owner lineage has duplicate or missing identities")
        reachable = {self.current_owner, implicit_owner, *self.configured_prior_owners}
        if any(link.repository != self.repository or link.role != "review"
               or link.owner_id == self.current_owner for link in self.links):
            raise ValueError("retired owner link is outside the review lineage")
        for _ in range(MAX_LEGACY_REVIEW_OWNERS):
            new = {link.owner_id for link in self.links if link.successor_owner_id in reachable}
            if new <= reachable:
                break
            reachable.update(new)
        if reachable != set(self.owners) or len({link.owner_id for link in self.links}) != len(self.links):
            raise ValueError("owner lineage is incomplete or has disconnected receipts")
        successor_by_owner = {link.owner_id: link.successor_owner_id for link in self.links}
        for owner in successor_by_owner:
            path: set[str] = set()
            current = owner
            while current in successor_by_owner:
                if current in path:
                    raise ValueError("retired owner lineage contains a cycle")
                path.add(current)
                current = successor_by_owner[current]

    @property
    def source_digest(self) -> str:
        payload = (self.repository, self.current_owner, self.configured_prior_owners,
                   tuple(sorted((link.owner_id, link.source_digest) for link in self.links)))
        return hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class UnreadableLegacyReviewOwnerLineage:
    reason: str
    source_rowid: int | None = None

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "unreadable owner lineage reason")
        if self.source_rowid is not None:
            _rowid(self.source_rowid, "unreadable retired owner cursor")


def _source_digest(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be a SHA-256 digest")


@dataclass(frozen=True)
class VerifiedLegacyRecordedReview:
    """A plain recorded baseline; execution productivity and round order remain unproved."""

    source: LegacyReviewSubmissionSource
    lineage: LegacyReviewOwnerLineage
    submission: ReviewSubmissionReceipt
    expected_revision: WorkflowRevision
    recorded_revision: WorkflowRevision
    baseline: ReviewedBaseline
    audit: ObservedTransition
    recorded_source_digest: str
    transition_source_digest: str

    def __post_init__(self) -> None:
        if (not isinstance(self.source, LegacyReviewSubmissionSource)
                or not isinstance(self.lineage, LegacyReviewOwnerLineage)
                or not isinstance(self.submission, ReviewSubmissionReceipt)
                or not isinstance(self.expected_revision, WorkflowRevision)
                or not isinstance(self.recorded_revision, WorkflowRevision)
                or not isinstance(self.baseline, ReviewedBaseline)
                or not isinstance(self.audit, ObservedTransition)):
            raise ValueError("verified recorded review requires typed sources")
        for name in ("recorded_source_digest", "transition_source_digest"):
            _source_digest(getattr(self, name), name)
        command = dict(self.source.command_fields)
        effect = dict(self.source.receipt_fields) if self.source.receipt_fields is not None else None
        binding = self.submission.submission.binding
        key = binding.fence.key
        if (self.submission.escalated
                or not isinstance(key.target, PullRequestRef) or key.repository != self.lineage.repository
                or command["command_id"] != str(self.submission.submission.command_id)
                or command["repository"] != key.repository
                or command["executor_identity"] not in self.lineage.owners
                or effect is None or effect["status"] != "succeeded"
                or effect["executor_identity"] not in self.lineage.owners
                or effect["remote_object_id"] != self.submission.receipt_id.removeprefix("github-review:")
                or self.expected_revision.fence != self.recorded_revision.fence
                or self.expected_revision.version + 1 != self.recorded_revision.version
                or self.baseline.reviewed_generation != self.recorded_revision.fence.generation
                or self.baseline.source != self.submission.submission
                or self.baseline.head_sha != self.submission.head_sha
                or self.baseline.patch_id != self.submission.patch_id
                or self.audit.evidence_ids != (str(self.submission.submission.command_id), self.submission.receipt_id)):
            raise ValueError("legacy sources do not prove one plain recorded baseline")

    @property
    def source_digest(self) -> str:
        parts = (self.source.source_digest, self.lineage.source_digest,
                 self.recorded_source_digest, self.transition_source_digest)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class UnverifiedLegacyRecordedReview:
    command_id: str
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "unverified review command")
        validate_workflow_text(self.reason, "unverified recorded review reason")


@dataclass(frozen=True)
class VerifiedReviewSubmissionRoundLink:
    """One SQL-backed command-to-round link; kind and productivity remain unproved."""

    source: LegacyReviewSubmissionSource
    lineage: LegacyReviewOwnerLineage
    round: ReviewRoundIdentity
    registration_source_digest: str
    index_source_digest: str
    preflight_source_digest: str
    stage_sources_digest: str
    round_source_digest: str

    def __post_init__(self) -> None:
        if (not isinstance(self.source, LegacyReviewSubmissionSource)
                or not isinstance(self.lineage, LegacyReviewOwnerLineage)
                or not isinstance(self.round, ReviewRoundIdentity)):
            raise ValueError("verified review round link requires typed sources")
        for name in ("registration_source_digest", "index_source_digest",
                     "preflight_source_digest", "stage_sources_digest", "round_source_digest"):
            _source_digest(getattr(self, name), name)
        command = dict(self.source.command_fields)
        key = self.round.binding.rejection.fence.key
        if (command["repository"] != self.lineage.repository
                or key.repository != self.lineage.repository
                or not isinstance(key.target, PullRequestRef)
                or command["target"] != f"pr:{key.target.number}"
                or command["executor_identity"] not in self.lineage.owners):
            raise ValueError("review submission sources do not prove one round link")

    @property
    def source_digest(self) -> str:
        parts = (self.source.source_digest, self.lineage.source_digest,
                 self.registration_source_digest, self.index_source_digest,
                 self.preflight_source_digest, self.stage_sources_digest, self.round_source_digest)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class UnverifiedReviewSubmissionRoundLink:
    command_id: str
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "unverified review command")
        validate_workflow_text(self.reason, "unverified review round link reason")


@dataclass(frozen=True)
class LegacyReviewRunNoDeath:
    source: LegacyExecutionRunRecord

    def __post_init__(self) -> None:
        if not isinstance(self.source, LegacyExecutionRunRecord) or self.source.state is LegacyReviewRunState.ABANDONED:
            raise ValueError("a non-death run requires a non-abandoned source")


@dataclass(frozen=True)
class AttributedLegacyReviewDeath:
    source: LegacyExecutionRunRecord
    repository: str
    pr: int
    counter_key: int

    def __post_init__(self) -> None:
        if not isinstance(self.source, LegacyExecutionRunRecord) or self.source.state is not LegacyReviewRunState.ABANDONED:
            raise ValueError("a review death requires an abandoned source")
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("review death repository must be canonical")
        validate_workflow_integer(self.pr, "review death PR", minimum=1)
        if type(self.counter_key) is not int or self.counter_key == 0 or abs(self.counter_key) > MAX_SQL_ROWID:
            raise ValueError("review death requires a valid signed counter key")
        if self.counter_key < 0 and self.counter_key != -self.pr:
            raise ValueError("negative review death counter must name its PR")
        if _dispatch_identity(self.source) != (self.repository, self.pr, self.counter_key):
            raise ValueError("review death attribution disagrees with its source")


@dataclass(frozen=True)
class UnattributedLegacyReviewDeath:
    source: LegacyExecutionRunRecord
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, LegacyExecutionRunRecord) or self.source.state is not LegacyReviewRunState.ABANDONED:
            raise ValueError("unattributed review death requires an abandoned source")
        validate_workflow_text(self.reason, "unattributed review death reason")


LegacyReviewDeathClassification = LegacyReviewRunNoDeath | AttributedLegacyReviewDeath | UnattributedLegacyReviewDeath


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate dispatch field")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise ValueError(f"nonfinite dispatch value: {value}")


def _dispatch_identity(source: LegacyExecutionRunRecord) -> tuple[str, int, int] | str:
    try:
        payload = json.loads(
            source.dispatch_id, object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (ValueError, TypeError, RecursionError):
        return "dispatch identity is not unambiguous JSON"
    if not isinstance(payload, dict):
        return "dispatch identity is not an object"
    repository = payload.get("repository")
    try:
        if not isinstance(repository, str) or canonical_workflow_repository(repository) != repository:
            raise ValueError("repository")
    except ValueError:
        return "dispatch repository is not canonical"
    kind = payload.get("kind")
    if payload.get("role") != "review" or not isinstance(kind, str) or kind not in {"REVIEW", "RE_REVIEW"}:
        return "dispatch is not an identified review leg"
    if source.kind != kind:
        return "dispatch kind disagrees with its run"
    pr, key = payload.get("number"), payload.get("breaker_key")
    if type(pr) is not int or not 1 <= pr <= MAX_SQL_ROWID:
        return "dispatch pull request number is invalid"
    if type(key) is not int or key == 0 or abs(key) > MAX_SQL_ROWID or (key < 0 and key != -pr):
        return "dispatch counter key is invalid or names another PR"
    if key > 0 and (type(payload.get("issue")) is not int or payload["issue"] != key):
        return "positive dispatch counter lacks its matching issue provenance"
    return repository, pr, key


def classify_legacy_review_death(source: LegacyExecutionRunRecord) -> LegacyReviewDeathClassification:
    """Classify only an abandoned run with exact PR and counter provenance.

    A positive issue counter still requires separate issue-to-PR mapping;
    the caller must verify the parsed repository against authorized owner
    lineage. This fact never imports a debit or treats completion as a reset.
    """
    if not isinstance(source, LegacyExecutionRunRecord):
        raise ValueError("review death classification requires a typed run source")
    if source.state is not LegacyReviewRunState.ABANDONED:
        return LegacyReviewRunNoDeath(source)
    identity = _dispatch_identity(source)
    if isinstance(identity, str):
        return UnattributedLegacyReviewDeath(source, identity)
    return AttributedLegacyReviewDeath(source, *identity)
