"""Bounded, read-only census pages for retained review round receipts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..domain.review_receipt_attribution import (
    BoundReviewRoundReceipt, CommandReviewRoundReceipt, NamedReviewRoundReceipt,
    ReviewRoundReceiptNamespace, ReviewRoundReceiptReference,
    classify_review_round_receipt,
)
from ..domain.review_execution import ReviewRoundIdentity
from ..domain.review_rounds import ReviewSubmissionRegistration, StaleReviewProductivitySettlement
from ..domain.review_round_receipt_archive import (
    ROUND_RECEIPT_NAMESPACES, ReviewRoundReceiptSweepCursor,
)
from ..domain.workflow_identity import WorkflowRevision
from .review_execution_activation import (
    MAX_CHECKPOINT_ROUND_RECEIPT_BYTES, MAX_LEGACY_RECORDING_BYTES,
    ObservedReviewCheckpointRoundReceiptMember,
    ObservedReviewCheckpointRoundReceiptSet, _DEDUPE_SOURCE_COLUMNS, _printable,
    _read_recording_source, _recording_source_size,
    read_checkpoint_round_receipt_members,
)
from .control_store import _parse_stamp
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import (
    WorkflowAuthorityError, review_stage_receipt_key, review_submission_receipt_key,
)


MAX_ROUND_RECEIPT_SWEEP_PAGE = 16
MAX_ROUND_RECEIPT_SWEEP_BYTES = 2**24
MAX_ROUND_RECEIPT_IDENTITY_BYTES = 512


@dataclass(frozen=True)
class ObservedReviewRoundReceiptSweepSource:
    source_rowid: int
    cursor: ReviewRoundReceiptSweepCursor
    source_sha256: str
    claimed_at: str
    reusable_after: str | None
    payload_json: str

    def __post_init__(self) -> None:
        if (type(self.source_rowid) is not int
                or not isinstance(self.cursor, ReviewRoundReceiptSweepCursor)
                or not isinstance(self.source_sha256, str)
                or len(self.source_sha256) != 64
                or any(char not in "0123456789abcdef" for char in self.source_sha256)
                or not isinstance(self.claimed_at, str)
                or self.reusable_after is not None and not isinstance(self.reusable_after, str)
                or not isinstance(self.payload_json, str)):
            raise ValueError("round receipt sweep source requires a typed source fingerprint")


@dataclass(frozen=True)
class ObservedReviewRoundReceiptSweepPage:
    sources: tuple[ObservedReviewRoundReceiptSweepSource, ...]
    next_cursor: ReviewRoundReceiptSweepCursor | None
    upper_cursor: ReviewRoundReceiptSweepCursor | None
    has_more: bool

    def __post_init__(self) -> None:
        if (not isinstance(self.sources, tuple)
                or len(self.sources) > MAX_ROUND_RECEIPT_SWEEP_PAGE
                or any(not isinstance(source, ObservedReviewRoundReceiptSweepSource)
                       for source in self.sources)
                or self.next_cursor is not None and not isinstance(
                    self.next_cursor, ReviewRoundReceiptSweepCursor)
                or self.upper_cursor is not None and not isinstance(
                    self.upper_cursor, ReviewRoundReceiptSweepCursor)
                or type(self.has_more) is not bool
                or self.upper_cursor is None and (self.sources or self.next_cursor or self.has_more)
                or self.sources and self.next_cursor != self.sources[-1].cursor
                or self.has_more and self.next_cursor is None):
            raise ValueError("round receipt sweep page requires bounded typed observations")


@dataclass(frozen=True)
class BlockedReviewRoundReceiptSweepPage:
    reason: str
    source_rowid: int | None

    def __post_init__(self) -> None:
        if (not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512
                or self.source_rowid is not None and type(self.source_rowid) is not int):
            raise ValueError("blocked round receipt sweep requires a bounded reason")


def read_review_round_receipt_sweep_page(
    db: sqlite3.Connection,
    *, after: ReviewRoundReceiptSweepCursor | None = None,
    upper: ReviewRoundReceiptSweepCursor | None = None,
    limit: int | None = None,
) -> ObservedReviewRoundReceiptSweepPage | BlockedReviewRoundReceiptSweepPage:
    """Observe all selected namespaces across repositories in indexed key order.

    A page is source evidence only. A durable pass and expected-source join
    must independently account for all returned fingerprints.
    """
    page_limit = MAX_ROUND_RECEIPT_SWEEP_PAGE if limit is None else limit
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or after is not None and not isinstance(after, ReviewRoundReceiptSweepCursor)
            or upper is not None and not isinstance(upper, ReviewRoundReceiptSweepCursor)
            or after is not None and upper is None
            or after is not None and upper is not None and after > upper
            or type(page_limit) is not int
            or not 1 <= page_limit <= MAX_ROUND_RECEIPT_SWEEP_PAGE):
        raise ValueError("round receipt sweep requires a transaction and typed cursor")
    current_rowid: int | None = None
    names = ",".join("?" for _ in ROUND_RECEIPT_NAMESPACES)
    try:
        alias = db.execute(
            f"SELECT rowid FROM dedupe_keys WHERE namespace IN ({names}) LIMIT 1",
            tuple(name.encode() for name in ROUND_RECEIPT_NAMESPACES),
        ).fetchone()
        if alias is not None:
            return BlockedReviewRoundReceiptSweepPage(
                "round receipt namespace has a BLOB alias", alias["rowid"],
            )
        if upper is None:
            edge = db.execute(
                "SELECT rowid,typeof(scope) AS scope_type,typeof(key) AS key_type,"
                "CASE WHEN length(CAST(scope AS BLOB))<=? THEN scope END AS scope_value,"
                "CASE WHEN length(CAST(key AS BLOB))<=? THEN key END AS key_value,namespace "
                f"FROM dedupe_keys WHERE namespace IN ({names}) "
                "ORDER BY namespace DESC,scope DESC,key DESC LIMIT 1",
                (MAX_ROUND_RECEIPT_IDENTITY_BYTES, MAX_ROUND_RECEIPT_IDENTITY_BYTES,
                 *ROUND_RECEIPT_NAMESPACES),
            ).fetchone()
            if edge is None:
                return ObservedReviewRoundReceiptSweepPage((), None, None, False)
            current_rowid = edge["rowid"]
            if (edge["scope_type"] != "text" or edge["key_type"] != "text"
                    or edge["scope_value"] is None or edge["key_value"] is None):
                return BlockedReviewRoundReceiptSweepPage(
                    "round receipt sweep has an unreadable upper key", current_rowid,
                )
            upper = ReviewRoundReceiptSweepCursor(
                edge["namespace"], edge["scope_value"], edge["key_value"],
            )
        assert upper is not None
        sizes = "+".join(
            f"ifnull(length(CAST({column} AS BLOB)),0)" for column in _DEDUPE_SOURCE_COLUMNS
        )
        rows: list[sqlite3.Row] = []
        for namespace in ROUND_RECEIPT_NAMESPACES:
            if (namespace > upper.namespace or after is not None
                    and namespace < after.namespace):
                continue
            lower = "" if after is None or namespace != after.namespace else "AND (scope,key)>(?,?) "
            ceiling = "" if namespace != upper.namespace else "AND (scope,key)<=(?,?) "
            parameters: tuple[object, ...] = (namespace,)
            if lower:
                parameters = (*parameters, after.scope, after.key)
            if ceiling:
                parameters = (*parameters, upper.scope, upper.key)
            rows.extend(db.execute(
                "SELECT rowid,typeof(namespace) AS namespace_type,"
                "typeof(scope) AS scope_type,typeof(key) AS key_type,"
                "CASE WHEN length(CAST(scope AS BLOB))<=? THEN scope END AS scope_value,"
                "CASE WHEN length(CAST(key AS BLOB))<=? THEN key END AS key_value,"
                f"namespace,{sizes} AS source_bytes FROM dedupe_keys "
                f"WHERE namespace=? {lower}{ceiling}"
                "ORDER BY scope,key LIMIT ?",
                (MAX_ROUND_RECEIPT_IDENTITY_BYTES, MAX_ROUND_RECEIPT_IDENTITY_BYTES,
                 *parameters, page_limit + 1 - len(rows)),
            ).fetchall())
            if len(rows) > page_limit:
                break
        sources: list[ObservedReviewRoundReceiptSweepSource] = []
        cursor = after
        source_bytes = 0
        processed = 0
        for row in rows[:page_limit]:
            current_rowid = row["rowid"]
            if (row["namespace_type"] != "text" or row["scope_type"] != "text"
                    or row["key_type"] != "text" or row["scope_value"] is None
                    or row["key_value"] is None):
                return BlockedReviewRoundReceiptSweepPage(
                    "round receipt sweep has a malformed source identity", current_rowid,
                )
            if (type(row["source_bytes"]) is not int
                    or row["source_bytes"] > MAX_LEGACY_RECORDING_BYTES * 6):
                return BlockedReviewRoundReceiptSweepPage(
                    "round receipt sweep has an oversized source", current_rowid,
                )
            if source_bytes + row["source_bytes"] > MAX_ROUND_RECEIPT_SWEEP_BYTES:
                if processed:
                    break
                return BlockedReviewRoundReceiptSweepPage(
                    "round receipt exceeds the sweep page byte bound", current_rowid,
                )
            source_cursor = ReviewRoundReceiptSweepCursor(
                row["namespace"], row["scope_value"], row["key_value"],
            )
            fields, digest = _read_recording_source(
                db, source_cursor.namespace, source_cursor.scope, source_cursor.key,
            )
            values = dict(fields)
            sources.append(ObservedReviewRoundReceiptSweepSource(
                current_rowid, source_cursor, digest,
                values["claimed_at"], values["reusable_after"], values["payload_json"],
            ))
            source_bytes += row["source_bytes"]
            cursor = source_cursor
            processed += 1
        return ObservedReviewRoundReceiptSweepPage(
            tuple(sources), cursor, upper, processed < len(rows),
        )
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError,
            sqlite3.Error) as exc:
        return BlockedReviewRoundReceiptSweepPage(_printable(str(exc))[:512], current_rowid)


@dataclass(frozen=True)
class ClassifiedReviewRoundReceiptSweepSource:
    source: ObservedReviewRoundReceiptSweepSource
    reference: ReviewRoundReceiptReference

    def __post_init__(self) -> None:
        if (not isinstance(self.source, ObservedReviewRoundReceiptSweepSource)
                or not isinstance(self.reference, (
                    BoundReviewRoundReceipt, NamedReviewRoundReceipt,
                    CommandReviewRoundReceipt,
                ))):
            raise ValueError("classified round receipt requires source and typed reference")


@dataclass(frozen=True)
class ClassifiedReviewRoundReceiptSweepPage:
    sources: tuple[ClassifiedReviewRoundReceiptSweepSource, ...]
    next_cursor: ReviewRoundReceiptSweepCursor | None
    upper_cursor: ReviewRoundReceiptSweepCursor | None
    has_more: bool

    def __post_init__(self) -> None:
        if (not isinstance(self.sources, tuple)
                or len(self.sources) > MAX_ROUND_RECEIPT_SWEEP_PAGE
                or any(not isinstance(source, ClassifiedReviewRoundReceiptSweepSource)
                       for source in self.sources)
                or self.next_cursor is not None and not isinstance(
                    self.next_cursor, ReviewRoundReceiptSweepCursor)
                or self.upper_cursor is not None and not isinstance(
                    self.upper_cursor, ReviewRoundReceiptSweepCursor)
                or type(self.has_more) is not bool
                or self.upper_cursor is None and (self.sources or self.next_cursor or self.has_more)
                or self.sources and self.next_cursor != self.sources[-1].source.cursor
                or self.has_more and self.next_cursor is None):
            raise ValueError("classified round receipt page requires bounded typed observations")


def _classify_source_payload(
    cursor: ReviewRoundReceiptSweepCursor, claimed_at: str,
    reusable_after: str | None, payload_json: str,
) -> ReviewRoundReceiptReference:
    if reusable_after is not None:
        raise ValueError("round receipt has a reusable source")
    _parse_stamp(claimed_at)
    decoded = decode_workflow(
        payload_json,
        tuple | ReviewSubmissionRegistration | StaleReviewProductivitySettlement
        | WorkflowRevision,
    )
    if encode_workflow(decoded) != payload_json:
        raise ValueError("round receipt has a noncanonical payload")
    namespace = ReviewRoundReceiptNamespace(cursor.namespace)
    reference = classify_review_round_receipt(namespace, cursor.key, decoded)
    if (isinstance(reference, BoundReviewRoundReceipt)
            and reference.round.binding.rejection.fence.key.repository != cursor.scope):
        raise ValueError("round receipt has a foreign repository")
    if namespace is ReviewRoundReceiptNamespace.RECORDED:
        if decoded.fence.key.repository != cursor.scope:
            raise ValueError("recorded review receipt has a foreign repository")
    elif namespace in (
        ReviewRoundReceiptNamespace.SUPERSEDED_PRODUCTIVITY,
        ReviewRoundReceiptNamespace.CEILING_PRODUCTIVITY,
    ):
        if decoded[1].fence.key.repository != cursor.scope:
            raise ValueError("productive review receipt has a foreign repository")
        if namespace is ReviewRoundReceiptNamespace.CEILING_PRODUCTIVITY:
            decision = decoded[0]
            if (decision.prior.revision.fence.key.repository != cursor.scope
                    or decision.escalation.submission.binding.fence.key.repository
                    != cursor.scope):
                raise ValueError("ceiling review receipt has a foreign repository")
    if namespace is ReviewRoundReceiptNamespace.STAGE:
        round_id, stage = decoded
        if cursor.key != review_stage_receipt_key(round_id, stage.stage_id):
            raise ValueError("round stage receipt has a foreign derived key")
    elif namespace is ReviewRoundReceiptNamespace.SUBMISSION_INDEX:
        if cursor.key != review_submission_receipt_key(
            decoded.round.round_id, decoded.submission.command_id,
        ):
            raise ValueError("round submission index has a foreign derived key")
    return reference


def read_classified_review_round_receipt_sweep_page(
    db: sqlite3.Connection,
    *, after: ReviewRoundReceiptSweepCursor | None = None,
    upper: ReviewRoundReceiptSweepCursor | None = None,
    limit: int | None = None,
) -> ClassifiedReviewRoundReceiptSweepPage | BlockedReviewRoundReceiptSweepPage:
    """Decode each bounded source and identify its local round or command reference.

    The reference has no episode or checkpoint authority until joined to
    canonical round, command and accepted-transition sources.
    """
    page = read_review_round_receipt_sweep_page(db, after=after, upper=upper, limit=limit)
    if isinstance(page, BlockedReviewRoundReceiptSweepPage):
        return page
    classified: list[ClassifiedReviewRoundReceiptSweepSource] = []
    current_rowid: int | None = None
    try:
        for source in page.sources:
            current_rowid = source.source_rowid
            reference = _classify_source_payload(
                source.cursor, source.claimed_at,
                source.reusable_after, source.payload_json,
            )
            classified.append(ClassifiedReviewRoundReceiptSweepSource(source, reference))
        return ClassifiedReviewRoundReceiptSweepPage(
            tuple(classified), page.next_cursor, page.upper_cursor, page.has_more,
        )
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError,
            RecursionError, sqlite3.Error) as exc:
        return BlockedReviewRoundReceiptSweepPage(_printable(str(exc))[:512], current_rowid)


@dataclass(frozen=True)
class ObservedReviewCheckpointRoundClassifiedReceipt:
    member: ObservedReviewCheckpointRoundReceiptMember
    reference: ReviewRoundReceiptReference

    def __post_init__(self) -> None:
        if (not isinstance(self.member, ObservedReviewCheckpointRoundReceiptMember)
                or not isinstance(self.reference, (
                    BoundReviewRoundReceipt, NamedReviewRoundReceipt,
                    CommandReviewRoundReceipt,
                ))):
            raise ValueError("checkpoint receipt observation requires a member and reference")


@dataclass(frozen=True)
class ObservedReviewCheckpointRoundClassifiedReceiptSet:
    source: ObservedReviewCheckpointRoundReceiptSet
    receipts: tuple[ObservedReviewCheckpointRoundClassifiedReceipt, ...]
    source_bytes: int

    def __post_init__(self) -> None:
        if (not isinstance(self.source, ObservedReviewCheckpointRoundReceiptSet)
                or not isinstance(self.receipts, tuple)
                or any(not isinstance(item, ObservedReviewCheckpointRoundClassifiedReceipt)
                       for item in self.receipts)
                or tuple(item.member for item in self.receipts) != self.source.members
                or type(self.source_bytes) is not int
                or not self.source.source_bytes < self.source_bytes
                <= self.source.source_bytes + MAX_CHECKPOINT_ROUND_RECEIPT_BYTES):
            raise ValueError("checkpoint receipt classification requires its exact member set")
        round_id = self.source.source.round
        if any(
            isinstance(item.reference, BoundReviewRoundReceipt)
            and item.reference.round != round_id
            or isinstance(item.reference, NamedReviewRoundReceipt)
            and item.reference.round_id != round_id.round_id
            or isinstance(item.reference, CommandReviewRoundReceipt)
            and item.reference.command_id != item.member.key
            for item in self.receipts
        ):
            raise ValueError("checkpoint receipt classification has a foreign reference")


def read_classified_checkpoint_round_receipt_members(
    db: sqlite3.Connection, round_id: ReviewRoundIdentity,
    *, max_source_bytes: int | None = None,
) -> ObservedReviewCheckpointRoundClassifiedReceiptSet:
    """Check one finalized round's bounded exact receipt members and local payloads.

    This neither scans other receipts nor proves command, claim or checkpoint
    ancestry. The point reader enforces a source-byte bound before this second
    digest-checked read of the same bounded member set.
    """
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(round_id, ReviewRoundIdentity)
            or max_source_bytes is not None
            and (type(max_source_bytes) is not int or max_source_bytes < 1)):
        raise ValueError("round receipt member classification requires a transaction and round")
    observed = read_checkpoint_round_receipt_members(
        db, round_id, max_source_bytes=max_source_bytes,
    )
    repository = round_id.binding.rejection.fence.key.repository
    classified: list[ObservedReviewCheckpointRoundClassifiedReceipt] = []
    source_bytes = observed.source_bytes
    try:
        for member in observed.members:
            size = _recording_source_size(db, member.namespace, repository, member.key)
            if (type(size) is not int or size < 1
                    or max_source_bytes is not None
                    and source_bytes + size > max_source_bytes):
                raise WorkflowAuthorityError("checkpoint receipt classification exceeds its byte bound")
            fields, digest = _read_recording_source(
                db, member.namespace, repository, member.key,
            )
            if digest != member.source_sha256:
                raise ValueError("checkpoint round receipt changed during classification")
            values = dict(fields)
            reference = _classify_source_payload(
                ReviewRoundReceiptSweepCursor(member.namespace, repository, member.key),
                values["claimed_at"], values["reusable_after"], values["payload_json"],
            )
            classified.append(ObservedReviewCheckpointRoundClassifiedReceipt(member, reference))
            source_bytes += size
        return ObservedReviewCheckpointRoundClassifiedReceiptSet(
            observed, tuple(classified), source_bytes,
        )
    except WorkflowAuthorityError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError,
            RecursionError, sqlite3.Error) as exc:
        raise WorkflowAuthorityError("checkpoint round receipt payload is unreadable") from exc
