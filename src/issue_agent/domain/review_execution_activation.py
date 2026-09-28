"""Typed, untrusted legacy sources for review execution activation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json

from .resolve_activation import LegacyAbsent, LegacyFile, LegacyPresent, LegacyUnreadable, UnmappedLegacyEntry
from .workflow_identity import canonical_workflow_repository, validate_workflow_integer, validate_workflow_text


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
