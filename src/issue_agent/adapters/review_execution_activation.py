"""Read-only inventory of legacy review execution files across state roots.

This inventory retains raw observations. Mapping issue counters to PRs and
validating their values require SQL run history and fresh linked-issue evidence.
"""

from __future__ import annotations

import os
import re
import sqlite3
import hashlib
import json
from dataclasses import dataclass, replace

from ..domain.resolve_activation import LegacyAbsent, LegacyFile, UnmappedLegacyEntry
from ..domain.review_execution_activation import (
    MAX_LEGACY_SQL_TEXT_BYTES, MAX_SQL_ROWID, MIN_SQL_ROWID, LegacyExecutionCounterRecord,
    LegacyExecutionEntry, LegacyExecutionPRRecord,
    LegacyExecutionRunPage, LegacyExecutionRunRecord, LegacyReviewRunState, UnreadableLegacyExecutionRun,
    CompleteLegacyReviewRuns, BlockedLegacyReviewRuns,
    MAX_LEGACY_RUN_INVENTORY_ROWS, MAX_LEGACY_RUN_INVENTORY_BYTES,
    LEGACY_SUBMISSION_COMMAND_COLUMNS, LEGACY_SUBMISSION_RECEIPT_COLUMNS,
    LegacyReviewSubmissionPage, LegacyReviewSubmissionSource, UnreadableLegacyReviewSubmission,
    CompleteLegacyReviewSubmissions, BlockedLegacyReviewSubmissions,
    MAX_LEGACY_SUBMISSION_INVENTORY_ROWS, MAX_LEGACY_SUBMISSION_INVENTORY_BYTES,
    LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS, LEGACY_REVIEW_SUBMISSION_RECEIPT_NAMESPACES,
    LegacyReviewSubmissionReceiptPage, LegacyReviewSubmissionReceiptSource,
    UnreadableLegacyReviewSubmissionReceipt,
    CompleteLegacyReviewSubmissionReceipts, BlockedLegacyReviewSubmissionReceipts,
    MAX_LEGACY_SUBMISSION_RECEIPT_INVENTORY_ROWS, MAX_LEGACY_SUBMISSION_RECEIPT_INVENTORY_BYTES,
    MAX_LEGACY_REVIEW_OWNERS, LegacyReviewOwnerLink, LegacyReviewOwnerLineage,
    UnreadableLegacyReviewOwnerLineage,
    VerifiedLegacyRecordedReview, UnverifiedLegacyRecordedReview,
    VerifiedReviewSubmissionRoundLink, UnverifiedReviewSubmissionRoundLink,
    LEGACY_REVIEW_ROUND_COLUMNS, MAX_LEGACY_ROUND_JSON_BYTES, MAX_LEGACY_ROUND_PAGE_BYTES,
    LegacyReviewRoundPage, LegacyReviewRoundSource, UnreadableLegacyReviewRound,
    CompleteLegacyReviewRounds, BlockedLegacyReviewRounds,
    MAX_LEGACY_ROUND_INVENTORY_ROWS, MAX_LEGACY_ROUND_INVENTORY_BYTES,
    LEGACY_REVIEW_OUTCOME_RECEIPT_NAMESPACES, MAX_LEGACY_OUTCOME_RECEIPT_JSON_BYTES,
    LegacyReviewOutcomeReceiptPage, LegacyReviewOutcomeReceiptSource,
    UnreadableLegacyReviewOutcomeReceipt, BlockedLegacyReviewOutcomeReceiptPage,
    CompleteLegacyReviewOutcomeReceipts, BlockedLegacyReviewOutcomeReceipts,
    MAX_LEGACY_OUTCOME_RECEIPT_INVENTORY_ROWS, MAX_LEGACY_OUTCOME_RECEIPT_INVENTORY_BYTES,
    VerifiedLegacyRoundOutcomeStorage, UnverifiedLegacyRoundOutcomeStorage,
    CompleteLegacyRoundOutcomeStorage, BlockedLegacyRoundOutcomeStorage,
    VerifiedLegacyPlainProductiveRound, UnverifiedLegacyPlainProductiveRound,
    BlockedLegacySubmissionSettlementStorage,
    LegacyReviewGenerationReceiptSource, CompleteLegacyReviewGenerationReceipts,
    BlockedLegacyReviewGenerationReceipts, MAX_LEGACY_GENERATION_RECEIPT_ROWS,
    MAX_LEGACY_GENERATION_RECEIPT_BYTES, MAX_LEGACY_GENERATION_RECEIPT_JSON_BYTES,
)
from ..domain.contracts import CommandId, CommandKind, EffectReceipt, EffectStatus, LifecycleCommand
from ..domain.review_execution import (
    ExecutionNotActivated, ActivatedReviewExecution, ReviewRoundOutcome,
    PaidReviewProgress, CopiedReviewProgress, ReviewExecutionGenerationBoundary,
)
from ..domain.review_workflow import ReviewWorkflowSnapshot
from ..domain.review_workflow_policy import ReviewSubmissionReceipt, CeilingProductivityDecision
from ..domain.review_rounds import (
    MAX_REVIEW_STAGES, ReviewRoundPlan, ReviewStagePlan, ReviewSubmissionKind, ReviewSubmissionRegistration,
    ReviewRoundFacts,
    DeferredReviewPreparation, ReviewSubmissionConfirmed, finalize_review_round,
)
from ..domain.review_preflight import ReviewPreflightSelection
from ..domain.workflow import (
    ObservedTransition, WorkflowMutation, AuthorizedOperatorCommand,
    RequestRearm, ContinueGrantedRearm,
)
from ..domain.workflow_identity import (
    PullRequestRef, WorkflowBinding, WorkflowRevision, MAX_REARM_COMMANDS,
    canonical_workflow_repository, validate_workflow_identifier,
)
from .control_store import CorruptControlStore, _encoded_payload_digest, _parse_stamp, _receipt_digest, load_bound_review_submission
from .resolve_activation import LegacyRoots, _printable, _read_legacy_file, distinct_roots
from .review_round_sources import _effect_receipt_id
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_commit import _unique_receipt_object
from .workflow_rows import (
    WorkflowAuthorityError, review_stage_receipt_key, review_stage_receipt_prefix,
    review_submission_receipt_key,
)

_PR_NAME = re.compile(r"pr-(.*)\.(session|round)", re.DOTALL)
_COUNTER_NAME = re.compile(r"issue-(.*)\.infra-deaths", re.DOTALL)
_POSITIVE = re.compile(r"[1-9][0-9]{0,18}")
_SIGNED = re.compile(r"-?[1-9][0-9]{0,18}")
_MAX_NUMBER = 2**63 - 1
MAX_RUN_SOURCE_PAGE = 256
MAX_LEGACY_RUN_SCAN_ROWS = 1_000_000
MAX_LEGACY_ROUND_SCAN_ROWS = 1_000_000
MAX_SUBMISSION_SOURCE_PAGE = 64
MAX_LEGACY_SUBMISSION_SCAN_ROWS = 1_000_000
MAX_SUBMISSION_RECEIPT_SOURCE_PAGE = 64
MAX_LEGACY_SUBMISSION_RECEIPT_SCAN_ROWS = 1_000_000
MAX_REVIEW_ROUND_SOURCE_PAGE = 16
MAX_OUTCOME_RECEIPT_SOURCE_PAGE = 16
MAX_OUTCOME_RECEIPT_PAGE_BYTES = 2**24
MAX_OUTCOME_RECEIPT_SCAN_ROWS = 1_000_000
MAX_OUTCOME_RECEIPT_SCAN_CHUNK = 256
MAX_LEGACY_GENERATION_RECEIPT_SCAN_ROWS = 1_000_000
_RETIRED_OWNER_COLUMNS = ("owner_id", "successor_owner_id", "repository", "role", "retired_at")
_DEDUPE_SOURCE_COLUMNS = ("namespace", "scope", "key", "claimed_at", "reusable_after", "payload_json")
MAX_LEGACY_RECORDING_BYTES = 2**21
_RUN_COLUMNS = (
    "run_id", "dispatch_id", "kind", "state", "config_version", "started_at", "completed_at", "outcome",
    "provider_receipt_id", "workflow_binding_json", "workflow_episode_key", "review_round_id", "review_child_id",
)


def _sql_text(kind: str, raw: bytes | None) -> str | None:
    if kind == "null" and raw is None:
        return None
    if kind != "text" or not isinstance(raw, bytes):
        raise ValueError("run source has a non-text field")
    return raw.decode("utf-8")


def _sql_integer(kind: str, raw: bytes | None) -> int:
    if kind != "integer" or not isinstance(raw, bytes):
        raise ValueError("submission source has a non-integer field")
    return int(raw.decode("ascii"))


def _number(raw: str, pattern: re.Pattern[str]) -> int | None:
    if pattern.fullmatch(raw) is None:
        return None
    number = int(raw)
    return number if abs(number) <= _MAX_NUMBER else None


def read_legacy_execution_state(state_dir: str, root: str = "review") -> tuple[LegacyExecutionEntry, ...]:
    """Every legacy review execution key in one labelled root, without writes."""
    try:
        names = sorted(entry.name for entry in os.scandir(state_dir))
    except FileNotFoundError:
        return ()
    except OSError as exc:
        return (UnmappedLegacyEntry("<state-dir>", _printable(f"state directory unreadable: {exc.strerror or exc}"), root),)

    pr_files: dict[int, dict[str, LegacyFile]] = {}
    counters: list[LegacyExecutionCounterRecord] = []
    unmapped: list[UnmappedLegacyEntry] = []
    for name in names:
        pr_match = _PR_NAME.fullmatch(name)
        if pr_match is not None:
            raw, suffix = pr_match.groups()
            pr = _number(raw, _POSITIVE)
            if pr is None:
                unmapped.append(UnmappedLegacyEntry(_printable(name), "not a canonical positive pull request number", root))
            else:
                pr_files.setdefault(pr, {})[suffix] = _read_legacy_file(os.path.join(state_dir, name))
            continue
        counter_match = _COUNTER_NAME.fullmatch(name)
        if counter_match is not None:
            key = _number(counter_match.group(1), _SIGNED)
            if key is None:
                unmapped.append(UnmappedLegacyEntry(_printable(name), "not a canonical signed issue or PR key", root))
            else:
                counters.append(LegacyExecutionCounterRecord(key, _read_legacy_file(os.path.join(state_dir, name)), root))

    prs = [LegacyExecutionPRRecord(pr, found.get("session", LegacyAbsent()), found.get("round", LegacyAbsent()), root)
           for pr, found in sorted(pr_files.items())]
    return tuple(prs + sorted(counters, key=lambda record: record.key) + unmapped)


def read_legacy_execution_roots(roots: LegacyRoots) -> tuple[LegacyExecutionEntry, ...]:
    """Retain duplicate targets in distinct roots for later ambiguity checks."""
    return tuple(entry for label, path in distinct_roots(roots) for entry in read_legacy_execution_state(path, label))


def _decode_run_source(
    owner_id: str, rowid: int, source: sqlite3.Row,
) -> LegacyExecutionRunRecord | UnreadableLegacyExecutionRun:
    if source["type_owner_id"] != "text" or source["raw_owner_id"] != owner_id.encode("utf-8"):
        return UnreadableLegacyExecutionRun(rowid, "run owner has a non-text or changed SQL identity")
    try:
        fields = {column: _sql_text(source[f"type_{column}"], source[f"raw_{column}"])
                  for column in _RUN_COLUMNS}
        state = LegacyReviewRunState(fields["state"])
        return LegacyExecutionRunRecord(
            rowid, owner_id, fields["run_id"], fields["dispatch_id"], fields["kind"], state,
            fields["config_version"], fields["started_at"], fields["completed_at"], fields["outcome"],
            fields["provider_receipt_id"], fields["workflow_binding_json"], fields["workflow_episode_key"],
            fields["review_round_id"], fields["review_child_id"],
        )
    except (TypeError, ValueError, UnicodeError):
        return UnreadableLegacyExecutionRun(rowid, "run source has malformed fields")


def _run_source_at_rowid(
    db: sqlite3.Connection, rowid: int, owner_id: str,
) -> LegacyExecutionRunRecord | UnreadableLegacyExecutionRun:
    sizes = ",".join(f"ifnull(length(CAST({column} AS BLOB)),0)" for column in ("owner_id", *_RUN_COLUMNS))
    metadata = db.execute(
        f"SELECT max({sizes}) AS largest_value FROM runs WHERE rowid=?", (rowid,),
    ).fetchone()
    if metadata is None or metadata["largest_value"] is None:
        return UnreadableLegacyExecutionRun(rowid, "run source changed during its read")
    if metadata["largest_value"] > MAX_LEGACY_SQL_TEXT_BYTES:
        return UnreadableLegacyExecutionRun(rowid, "run source field exceeds the SQL text bound")
    fields = "typeof(owner_id) AS type_owner_id,CAST(owner_id AS BLOB) AS raw_owner_id," + ",".join(
        f"typeof({column}) AS type_{column},CAST({column} AS BLOB) AS raw_{column}"
        for column in _RUN_COLUMNS
    )
    source = db.execute(f"SELECT {fields} FROM runs WHERE rowid=?", (rowid,)).fetchone()
    if source is None:
        return UnreadableLegacyExecutionRun(rowid, "run source changed during its read")
    return _decode_run_source(owner_id, rowid, source)


def read_legacy_review_run_page(
    db: sqlite3.Connection, owner_id: str, *, after_rowid: int | None = None,
) -> LegacyExecutionRunPage:
    """Read a bounded owner-run page under the caller's explicit SQL snapshot.

    The importer must finish all pages in one read transaction, include
    authorized predecessor owners, and revalidate under activation barriers.
    An unreadable row is retained as a blocker; malformed dispatch ids are
    preserved for attribution rather than skipped.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("legacy run scan requires an explicit SQL transaction")
    if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 512:
        raise ValueError("legacy run scan requires a bounded owner identity")
    try:
        owner_bytes = owner_id.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("legacy run scan requires a UTF-8 owner identity") from exc
    if after_rowid is not None and (type(after_rowid) is not int or not MIN_SQL_ROWID <= after_rowid <= MAX_SQL_ROWID):
        raise ValueError("legacy run scan requires a valid cursor")

    sizes = ",".join(f"ifnull(length(CAST({column} AS BLOB)),0)" for column in ("owner_id", *_RUN_COLUMNS))
    predicate = "WHERE owner_id IN (?,?)" + (" AND rowid>?" if after_rowid is not None else "")
    params = (owner_id, owner_bytes) if after_rowid is None else (
        owner_id, owner_bytes, after_rowid,
    )
    metadata = db.execute(
        f"SELECT rowid AS source_rowid,max({sizes}) AS largest_value FROM runs "
        f"{predicate} ORDER BY rowid LIMIT ?",
        (*params, MAX_RUN_SOURCE_PAGE + 1),
    ).fetchall()
    page = metadata[:MAX_RUN_SOURCE_PAGE]
    safe_ids = [row["source_rowid"] for row in page if row["largest_value"] <= MAX_LEGACY_SQL_TEXT_BYTES]
    values = {}
    if safe_ids:
        placeholders = ",".join("?" for _ in safe_ids)
        fields = "typeof(owner_id) AS type_owner_id,CAST(owner_id AS BLOB) AS raw_owner_id," + ",".join(
            f"typeof({column}) AS type_{column},CAST({column} AS BLOB) AS raw_{column}"
            for column in _RUN_COLUMNS
        )
        values = {row["source_rowid"]: row for row in db.execute(
            f"SELECT rowid AS source_rowid,{fields} FROM runs "
            f"WHERE owner_id IN (?,?) AND rowid IN ({placeholders})",
            (owner_id, owner_bytes, *safe_ids),
        )}
    entries: list[LegacyExecutionRunRecord | UnreadableLegacyExecutionRun] = []
    for row in page:
        rowid = row["source_rowid"]
        if row["largest_value"] > MAX_LEGACY_SQL_TEXT_BYTES:
            entries.append(UnreadableLegacyExecutionRun(rowid, "run source field exceeds the SQL text bound"))
            continue
        source = values.get(rowid)
        if source is None:
            entries.append(UnreadableLegacyExecutionRun(rowid, "run source changed during its read"))
            continue
        entries.append(_decode_run_source(owner_id, rowid, source))
    return LegacyExecutionRunPage(tuple(entries), page[-1]["source_rowid"] if page else after_rowid,
                                  len(metadata) > MAX_RUN_SOURCE_PAGE)


def read_complete_legacy_review_runs(
    db: sqlite3.Connection, lineage: LegacyReviewOwnerLineage,
) -> CompleteLegacyReviewRuns | BlockedLegacyReviewRuns:
    """Inspect bounded run rows once in a read snapshot, or return a blocker.

    Activation must re-run this scan under its deployment barriers and compare
    source digests. No partial result is eligible for import.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("complete legacy run scan requires an explicit SQL transaction")
    if not isinstance(lineage, LegacyReviewOwnerLineage):
        raise ValueError("complete legacy run scan requires a typed owner lineage")
    current = read_legacy_review_owner_lineage(
        db, lineage.repository, lineage.current_owner,
        configured_prior_owners=lineage.configured_prior_owners,
    )
    if current != lineage:
        return BlockedLegacyReviewRuns(lineage, "owner lineage changed after inventory")
    owner_by_bytes = {owner.encode("utf-8"): owner for owner in lineage.owners}
    if any(len(owner) > 512 for owner in owner_by_bytes):
        return BlockedLegacyReviewRuns(lineage, "review owner identity exceeds the scan byte bound")
    runs: list[LegacyExecutionRunRecord] = []
    source_bytes = 0
    examined = 0
    cursor = None
    while True:
        predicate = "" if cursor is None else "WHERE rowid>?"
        params = () if cursor is None else (cursor,)
        metadata = db.execute(
            "SELECT rowid AS source_rowid, "
            "CASE WHEN length(CAST(owner_id AS BLOB))<=512 THEN CAST(owner_id AS BLOB) END AS owner_bytes "
            f"FROM runs {predicate} ORDER BY rowid LIMIT ?",
            (*params, MAX_RUN_SOURCE_PAGE + 1),
        ).fetchall()
        page = metadata[:MAX_RUN_SOURCE_PAGE]
        if examined + len(page) > MAX_LEGACY_RUN_SCAN_ROWS:
            return BlockedLegacyReviewRuns(lineage, "review run table exceeds its scan bound",
                                           source_rowid=page[0]["source_rowid"])
        examined += len(page)
        for row in page:
            owner = owner_by_bytes.get(row["owner_bytes"])
            if owner is None:
                continue
            entry = _run_source_at_rowid(db, row["source_rowid"], owner)
            if isinstance(entry, UnreadableLegacyExecutionRun):
                return BlockedLegacyReviewRuns(lineage, entry.reason, owner, entry.source_rowid)
            source_bytes += entry.source_bytes
            if len(runs) >= MAX_LEGACY_RUN_INVENTORY_ROWS or source_bytes > MAX_LEGACY_RUN_INVENTORY_BYTES:
                return BlockedLegacyReviewRuns(lineage, "review run inventory exceeds its total bound",
                                               owner, entry.source_rowid)
            runs.append(entry)
        if len(metadata) <= MAX_RUN_SOURCE_PAGE:
            break
        cursor = page[-1]["source_rowid"]
    try:
        return CompleteLegacyReviewRuns(lineage, tuple(runs))
    except ValueError as exc:
        return BlockedLegacyReviewRuns(lineage, str(exc)[:512])


def revalidate_complete_legacy_review_runs(
    db: sqlite3.Connection, observed: CompleteLegacyReviewRuns,
) -> CompleteLegacyReviewRuns | BlockedLegacyReviewRuns:
    """Recheck a caller-held inventory against SQL before activation accepts it."""
    if not isinstance(observed, CompleteLegacyReviewRuns):
        raise ValueError("run revalidation requires a complete observed inventory")
    current = read_complete_legacy_review_runs(db, observed.lineage)
    if isinstance(current, BlockedLegacyReviewRuns):
        return current
    if current.source_digest != observed.source_digest:
        return BlockedLegacyReviewRuns(observed.lineage, "review run sources changed after inventory")
    return current


def read_legacy_review_submission_page(
    db: sqlite3.Connection, repository: str, *, after_rowid: int | None = None,
) -> LegacyReviewSubmissionPage:
    """Bounded review submission commands and effect receipts in one SQL snapshot.

    Missing effect receipts are retained, not inferred from command state.
    The importer must read every page and separately inspect round submission
    indices and remote verdict provenance before deciding productive resets.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("submission scan requires an explicit SQL transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("submission scan requires a canonical repository")
    if after_rowid is not None and (type(after_rowid) is not int or not MIN_SQL_ROWID <= after_rowid <= MAX_SQL_ROWID):
        raise ValueError("submission scan requires a valid cursor")

    command_sizes = [f"ifnull(length(CAST(c.{column} AS BLOB)),0)" for column in LEGACY_SUBMISSION_COMMAND_COLUMNS]
    receipt_sizes = [f"ifnull(length(CAST(e.{column} AS BLOB)),0)" for column in LEGACY_SUBMISSION_RECEIPT_COLUMNS]
    sizes = ",".join((*command_sizes, *receipt_sizes))
    predicate = "WHERE c.repository IN (?,?) AND c.kind IN (?,?)" + (
        " AND c.rowid>?" if after_rowid is not None else ""
    )
    params = (repository, repository.encode("utf-8"), "submit_review", b"submit_review")
    if after_rowid is not None:
        params = (*params, after_rowid)
    metadata = db.execute(
        f"SELECT c.rowid AS source_rowid,max({sizes}) AS largest_value "
        "FROM lifecycle_commands c NOT INDEXED LEFT JOIN effect_receipts e ON e.command_id=c.command_id "
        f"{predicate} ORDER BY c.rowid LIMIT ?",
        (*params, MAX_SUBMISSION_SOURCE_PAGE + 1),
    ).fetchall()
    page = metadata[:MAX_SUBMISSION_SOURCE_PAGE]
    safe_ids = [row["source_rowid"] for row in page if row["largest_value"] <= MAX_LEGACY_SQL_TEXT_BYTES]
    values = {}
    if safe_ids:
        placeholders = ",".join("?" for _ in safe_ids)
        command_fields = ",".join(
            f"typeof(c.{column}) AS command_type_{column},CAST(c.{column} AS BLOB) AS command_raw_{column}"
            for column in LEGACY_SUBMISSION_COMMAND_COLUMNS
        )
        receipt_fields = ",".join(
            f"typeof(e.{column}) AS receipt_type_{column},CAST(e.{column} AS BLOB) AS receipt_raw_{column}"
            for column in LEGACY_SUBMISSION_RECEIPT_COLUMNS
        )
        values = {row["source_rowid"]: row for row in db.execute(
            f"SELECT c.rowid AS source_rowid,{command_fields},{receipt_fields} "
            "FROM lifecycle_commands c LEFT JOIN effect_receipts e ON e.command_id=c.command_id "
            f"WHERE c.repository IN (?,?) AND c.kind IN (?,?) AND c.rowid IN ({placeholders})",
            (repository, repository.encode("utf-8"), "submit_review", b"submit_review", *safe_ids),
        )}
    entries: list[LegacyReviewSubmissionSource | UnreadableLegacyReviewSubmission] = []
    for row in page:
        rowid = row["source_rowid"]
        if row["largest_value"] > MAX_LEGACY_SQL_TEXT_BYTES:
            entries.append(UnreadableLegacyReviewSubmission(rowid, "submission source field exceeds the SQL text bound"))
            continue
        source = values.get(rowid)
        if source is None:
            entries.append(UnreadableLegacyReviewSubmission(rowid, "submission source changed during its read"))
            continue
        try:
            integers = {"occurrence", "attempts", "refusals"}
            command = tuple(
                (column, (_sql_integer if column in integers else _sql_text)(
                    source[f"command_type_{column}"], source[f"command_raw_{column}"],
                )) for column in LEGACY_SUBMISSION_COMMAND_COLUMNS
            )
            receipt = None if source["receipt_type_command_id"] == "null" else tuple(
                (column, _sql_text(source[f"receipt_type_{column}"], source[f"receipt_raw_{column}"]))
                for column in LEGACY_SUBMISSION_RECEIPT_COLUMNS
            )
            entries.append(LegacyReviewSubmissionSource(rowid, command, receipt))
        except (TypeError, ValueError, UnicodeError):
            entries.append(UnreadableLegacyReviewSubmission(rowid, "submission source has malformed fields"))
    return LegacyReviewSubmissionPage(tuple(entries), page[-1]["source_rowid"] if page else after_rowid,
                                      len(metadata) > MAX_SUBMISSION_SOURCE_PAGE)


def read_complete_legacy_review_submissions(
    db: sqlite3.Connection, repository: str,
) -> CompleteLegacyReviewSubmissions | BlockedLegacyReviewSubmissions:
    """Scan every review submission command; partial pages cannot authorize import."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("complete submission scan requires an explicit SQL transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("complete submission scan requires a canonical repository")
    if db.execute(
        "SELECT rowid FROM lifecycle_commands NOT INDEXED ORDER BY rowid LIMIT 1 OFFSET ?",
        (MAX_LEGACY_SUBMISSION_SCAN_ROWS,),
    ).fetchone() is not None:
        return BlockedLegacyReviewSubmissions(repository, "lifecycle command table exceeds its total scan bound")
    submissions: list[LegacyReviewSubmissionSource] = []
    source_bytes = 0
    cursor = None
    while True:
        page = read_legacy_review_submission_page(db, repository, after_rowid=cursor)
        for entry in page.entries:
            if isinstance(entry, UnreadableLegacyReviewSubmission):
                return BlockedLegacyReviewSubmissions(repository, entry.reason, entry.source_rowid)
            source_bytes += entry.source_bytes
            if (len(submissions) >= MAX_LEGACY_SUBMISSION_INVENTORY_ROWS
                    or source_bytes > MAX_LEGACY_SUBMISSION_INVENTORY_BYTES):
                return BlockedLegacyReviewSubmissions(
                    repository, "review submission inventory exceeds its total bound", entry.source_rowid,
                )
            submissions.append(entry)
        if not page.has_more:
            break
        cursor = page.next_rowid
    try:
        return CompleteLegacyReviewSubmissions(repository, tuple(submissions))
    except ValueError as exc:
        return BlockedLegacyReviewSubmissions(repository, _printable(str(exc)))


def revalidate_complete_legacy_review_submissions(
    db: sqlite3.Connection, observed: CompleteLegacyReviewSubmissions,
) -> CompleteLegacyReviewSubmissions | BlockedLegacyReviewSubmissions:
    """Re-read the whole command source set before using a caller-held inventory."""
    if not isinstance(observed, CompleteLegacyReviewSubmissions):
        raise ValueError("submission revalidation requires a complete observed inventory")
    current = read_complete_legacy_review_submissions(db, observed.repository)
    if isinstance(current, BlockedLegacyReviewSubmissions):
        return current
    if current.source_digest != observed.source_digest:
        return BlockedLegacyReviewSubmissions(observed.repository, "review submission sources changed after inventory")
    return current


def read_legacy_review_submission_receipt_page(
    db: sqlite3.Connection, repository: str, *, after_rowid: int | None = None,
) -> LegacyReviewSubmissionReceiptPage:
    """Inventory registration, round-index and recorded-review receipts.

    The importer must join all pages to command/effect and round sources;
    a recorded-review row by itself does not prove a productive reset.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("submission receipt scan requires an explicit SQL transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("submission receipt scan requires a canonical repository")
    if after_rowid is not None and (type(after_rowid) is not int or not MIN_SQL_ROWID <= after_rowid <= MAX_SQL_ROWID):
        raise ValueError("submission receipt scan requires a valid cursor")

    columns = LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS
    sizes = ",".join(f"ifnull(length(CAST({column} AS BLOB)),0)" for column in columns)
    namespaces = tuple(sorted(LEGACY_REVIEW_SUBMISSION_RECEIPT_NAMESPACES))
    namespace_values = (*namespaces, *(name.encode("utf-8") for name in namespaces))
    placeholders = ",".join("?" for _ in namespace_values)
    predicate = f"WHERE scope IN (?,?) AND namespace IN ({placeholders})" + (
        " AND rowid>?" if after_rowid is not None else ""
    )
    params = (repository, repository.encode("utf-8"), *namespace_values)
    if after_rowid is not None:
        params = (*params, after_rowid)
    metadata = db.execute(
        f"SELECT rowid AS source_rowid,max({sizes}) AS largest_value FROM dedupe_keys "
        f"NOT INDEXED {predicate} ORDER BY rowid LIMIT ?",
        (*params, MAX_SUBMISSION_RECEIPT_SOURCE_PAGE + 1),
    ).fetchall()
    page = metadata[:MAX_SUBMISSION_RECEIPT_SOURCE_PAGE]
    safe_ids = [row["source_rowid"] for row in page if row["largest_value"] <= MAX_LEGACY_SQL_TEXT_BYTES]
    values = {}
    if safe_ids:
        rowids = ",".join("?" for _ in safe_ids)
        fields = ",".join(
            f"typeof({column}) AS type_{column},CAST({column} AS BLOB) AS raw_{column}"
            for column in columns
        )
        values = {row["source_rowid"]: row for row in db.execute(
            f"SELECT rowid AS source_rowid,{fields} FROM dedupe_keys "
            f"WHERE scope IN (?,?) AND namespace IN ({placeholders}) AND rowid IN ({rowids})",
            (repository, repository.encode("utf-8"), *namespace_values, *safe_ids),
        )}
    entries: list[LegacyReviewSubmissionReceiptSource | UnreadableLegacyReviewSubmissionReceipt] = []
    for row in page:
        rowid = row["source_rowid"]
        if row["largest_value"] > MAX_LEGACY_SQL_TEXT_BYTES:
            entries.append(UnreadableLegacyReviewSubmissionReceipt(rowid, "submission receipt field exceeds the SQL text bound"))
            continue
        source = values.get(rowid)
        if source is None:
            entries.append(UnreadableLegacyReviewSubmissionReceipt(rowid, "submission receipt changed during its read"))
            continue
        try:
            fields = tuple((column, _sql_text(source[f"type_{column}"], source[f"raw_{column}"]))
                           for column in columns)
            entries.append(LegacyReviewSubmissionReceiptSource(rowid, fields))
        except (TypeError, ValueError, UnicodeError):
            entries.append(UnreadableLegacyReviewSubmissionReceipt(rowid, "submission receipt has malformed fields"))
    return LegacyReviewSubmissionReceiptPage(
        tuple(entries), page[-1]["source_rowid"] if page else after_rowid,
        len(metadata) > MAX_SUBMISSION_RECEIPT_SOURCE_PAGE,
    )


def read_complete_legacy_review_submission_receipts(
    db: sqlite3.Connection, repository: str,
) -> CompleteLegacyReviewSubmissionReceipts | BlockedLegacyReviewSubmissionReceipts:
    """Scan every settlement receipt; partial pages cannot authorize import."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("complete submission receipt scan requires an explicit SQL transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("complete submission receipt scan requires a canonical repository")
    if db.execute(
        "SELECT rowid FROM dedupe_keys NOT INDEXED ORDER BY rowid LIMIT 1 OFFSET ?",
        (MAX_LEGACY_SUBMISSION_RECEIPT_SCAN_ROWS,),
    ).fetchone() is not None:
        return BlockedLegacyReviewSubmissionReceipts(repository, "dedupe table exceeds its total scan bound")
    receipts: list[LegacyReviewSubmissionReceiptSource] = []
    source_bytes = 0
    cursor = None
    while True:
        page = read_legacy_review_submission_receipt_page(db, repository, after_rowid=cursor)
        for entry in page.entries:
            if isinstance(entry, UnreadableLegacyReviewSubmissionReceipt):
                return BlockedLegacyReviewSubmissionReceipts(repository, entry.reason, entry.source_rowid)
            source_bytes += entry.source_bytes
            if (len(receipts) >= MAX_LEGACY_SUBMISSION_RECEIPT_INVENTORY_ROWS
                    or source_bytes > MAX_LEGACY_SUBMISSION_RECEIPT_INVENTORY_BYTES):
                return BlockedLegacyReviewSubmissionReceipts(
                    repository, "submission receipt inventory exceeds its total bound", entry.source_rowid,
                )
            receipts.append(entry)
        if not page.has_more:
            break
        cursor = page.next_rowid
    try:
        return CompleteLegacyReviewSubmissionReceipts(repository, tuple(receipts))
    except ValueError as exc:
        return BlockedLegacyReviewSubmissionReceipts(repository, _printable(str(exc)))


def revalidate_complete_legacy_review_submission_receipts(
    db: sqlite3.Connection, observed: CompleteLegacyReviewSubmissionReceipts,
) -> CompleteLegacyReviewSubmissionReceipts | BlockedLegacyReviewSubmissionReceipts:
    """Re-read every settlement receipt before trusting a caller-held set."""
    if not isinstance(observed, CompleteLegacyReviewSubmissionReceipts):
        raise ValueError("submission receipt revalidation requires a complete observed inventory")
    current = read_complete_legacy_review_submission_receipts(db, observed.repository)
    if isinstance(current, BlockedLegacyReviewSubmissionReceipts):
        return current
    if current.source_digest != observed.source_digest:
        return BlockedLegacyReviewSubmissionReceipts(observed.repository,
                                                     "submission receipt sources changed after inventory")
    return current


def read_legacy_review_outcome_receipt_page(
    db: sqlite3.Connection, repository: str, *, after_rowid: int | None = None,
) -> LegacyReviewOutcomeReceiptPage | BlockedLegacyReviewOutcomeReceiptPage:
    """Inventory durable round outcome and productivity receipts without interpretation."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("review outcome receipt scan requires an explicit SQL transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("review outcome receipt scan requires a canonical repository")
    if after_rowid is not None and (type(after_rowid) is not int or not MIN_SQL_ROWID <= after_rowid <= MAX_SQL_ROWID):
        raise ValueError("review outcome receipt scan requires a valid cursor")
    repository_bytes = repository.encode("utf-8")
    if len(repository_bytes) > 512:
        raise ValueError("review outcome receipt repository exceeds the scan identity bound")
    namespace_bytes = {name.encode("utf-8") for name in LEGACY_REVIEW_OUTCOME_RECEIPT_NAMESPACES}
    matching_ids: list[int] = []
    examined = 0
    cursor = after_rowid
    exhausted = False
    while len(matching_ids) <= MAX_OUTCOME_RECEIPT_SOURCE_PAGE and not exhausted:
        remaining = MAX_OUTCOME_RECEIPT_SCAN_ROWS - examined
        if remaining == 0:
            predicate = "" if cursor is None else "WHERE rowid>?"
            params = () if cursor is None else (cursor,)
            if db.execute(
                f"SELECT rowid FROM dedupe_keys NOT INDEXED {predicate} ORDER BY rowid LIMIT 1", params,
            ).fetchone() is not None:
                return BlockedLegacyReviewOutcomeReceiptPage(
                    repository, "review outcome receipt page exceeds its total scan bound", cursor,
                )
            break
        predicate = "" if cursor is None else "WHERE rowid>?"
        params = () if cursor is None else (cursor,)
        batch = db.execute(
            "SELECT rowid AS source_rowid,"
            "CASE WHEN length(CAST(namespace AS BLOB))<=512 THEN CAST(namespace AS BLOB) END AS namespace_bytes,"
            "CASE WHEN length(CAST(scope AS BLOB))<=512 THEN CAST(scope AS BLOB) END AS scope_bytes "
            f"FROM dedupe_keys NOT INDEXED {predicate} ORDER BY rowid LIMIT ?",
            (*params, min(MAX_OUTCOME_RECEIPT_SCAN_CHUNK, remaining)),
        ).fetchall()
        if not batch:
            exhausted = True
            break
        for row in batch:
            cursor = row["source_rowid"]
            examined += 1
            if row["scope_bytes"] == repository_bytes and row["namespace_bytes"] in namespace_bytes:
                matching_ids.append(cursor)
                if len(matching_ids) > MAX_OUTCOME_RECEIPT_SOURCE_PAGE:
                    break
        exhausted = len(batch) < min(MAX_OUTCOME_RECEIPT_SCAN_CHUNK, remaining)

    other_columns = ("namespace", "scope", "key", "claimed_at", "reusable_after")
    other_sizes = [f"ifnull(length(CAST({column} AS BLOB)),0)" for column in other_columns]
    payload_size = "ifnull(length(CAST(payload_json AS BLOB)),0)"
    metadata = []
    if matching_ids:
        marks = ",".join("?" for _ in matching_ids)
        metadata = db.execute(
            f"SELECT rowid AS source_rowid,max({','.join(other_sizes)}) AS largest_other,"
            f"{payload_size} AS payload_bytes,{'+'.join((*other_sizes, payload_size))} AS total_bytes "
            f"FROM dedupe_keys WHERE rowid IN ({marks}) ORDER BY rowid", matching_ids,
        ).fetchall()
    selected: list[sqlite3.Row] = []
    retained_bytes = 0
    for row in metadata[:MAX_OUTCOME_RECEIPT_SOURCE_PAGE]:
        safe = (row["largest_other"] <= MAX_LEGACY_SQL_TEXT_BYTES
                and row["payload_bytes"] <= MAX_LEGACY_OUTCOME_RECEIPT_JSON_BYTES)
        if safe and selected and retained_bytes + row["total_bytes"] > MAX_OUTCOME_RECEIPT_PAGE_BYTES:
            break
        selected.append(row)
        if safe:
            retained_bytes += row["total_bytes"]
    safe_ids = [row["source_rowid"] for row in selected if (
        row["largest_other"] <= MAX_LEGACY_SQL_TEXT_BYTES
        and row["payload_bytes"] <= MAX_LEGACY_OUTCOME_RECEIPT_JSON_BYTES
        and row["total_bytes"] <= MAX_OUTCOME_RECEIPT_PAGE_BYTES
    )]
    values = {}
    if safe_ids:
        ids = ",".join("?" for _ in safe_ids)
        fields = ",".join(
            f"typeof({column}) AS type_{column},CAST({column} AS BLOB) AS raw_{column}"
            for column in LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS
        )
        values = {row["source_rowid"]: row for row in db.execute(
            f"SELECT rowid AS source_rowid,{fields} FROM dedupe_keys "
            f"WHERE rowid IN ({ids})", safe_ids,
        )}
    entries: list[LegacyReviewOutcomeReceiptSource | UnreadableLegacyReviewOutcomeReceipt] = []
    for row in selected:
        rowid = row["source_rowid"]
        if (row["largest_other"] > MAX_LEGACY_SQL_TEXT_BYTES
                or row["payload_bytes"] > MAX_LEGACY_OUTCOME_RECEIPT_JSON_BYTES
                or row["total_bytes"] > MAX_OUTCOME_RECEIPT_PAGE_BYTES):
            entries.append(UnreadableLegacyReviewOutcomeReceipt(rowid, "review outcome receipt exceeds its SQL source bound"))
            continue
        source = values.get(rowid)
        if source is None:
            entries.append(UnreadableLegacyReviewOutcomeReceipt(rowid, "review outcome receipt changed during its read"))
            continue
        try:
            fields = tuple((column, _sql_text(source[f"type_{column}"], source[f"raw_{column}"]))
                           for column in LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS)
            entries.append(LegacyReviewOutcomeReceiptSource(rowid, fields))
        except (TypeError, ValueError, UnicodeError):
            entries.append(UnreadableLegacyReviewOutcomeReceipt(rowid, "review outcome receipt has malformed fields"))
    return LegacyReviewOutcomeReceiptPage(
        tuple(entries), selected[-1]["source_rowid"] if selected else after_rowid,
        len(metadata) > len(selected),
    )


def read_complete_legacy_review_outcome_receipts(
    db: sqlite3.Connection, repository: str,
) -> CompleteLegacyReviewOutcomeReceipts | BlockedLegacyReviewOutcomeReceipts:
    """Exhaust receipt pages within one SQL snapshot and shared-table bound."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("complete outcome receipt scan requires an explicit SQL transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("complete outcome receipt scan requires a canonical repository")
    if db.execute(
        "SELECT rowid FROM dedupe_keys NOT INDEXED ORDER BY rowid LIMIT 1 OFFSET ?",
        (MAX_OUTCOME_RECEIPT_SCAN_ROWS,),
    ).fetchone() is not None:
        return BlockedLegacyReviewOutcomeReceipts(repository, "outcome receipt table exceeds its total scan bound")
    receipts: list[LegacyReviewOutcomeReceiptSource] = []
    source_bytes = 0
    cursor = None
    while True:
        page = read_legacy_review_outcome_receipt_page(db, repository, after_rowid=cursor)
        if isinstance(page, BlockedLegacyReviewOutcomeReceiptPage):
            return BlockedLegacyReviewOutcomeReceipts(repository, page.reason, page.source_rowid)
        for entry in page.entries:
            if isinstance(entry, UnreadableLegacyReviewOutcomeReceipt):
                return BlockedLegacyReviewOutcomeReceipts(repository, entry.reason, entry.source_rowid)
            source_bytes += entry.source_bytes
            if (len(receipts) >= MAX_LEGACY_OUTCOME_RECEIPT_INVENTORY_ROWS
                    or source_bytes > MAX_LEGACY_OUTCOME_RECEIPT_INVENTORY_BYTES):
                return BlockedLegacyReviewOutcomeReceipts(
                    repository, "outcome receipt inventory exceeds its total bound", entry.source_rowid,
                )
            receipts.append(entry)
        if not page.has_more:
            break
        cursor = page.next_rowid
    try:
        return CompleteLegacyReviewOutcomeReceipts(repository, tuple(receipts))
    except ValueError as exc:
        return BlockedLegacyReviewOutcomeReceipts(repository, str(exc)[:512])


def revalidate_complete_legacy_review_outcome_receipts(
    db: sqlite3.Connection, observed: CompleteLegacyReviewOutcomeReceipts,
) -> CompleteLegacyReviewOutcomeReceipts | BlockedLegacyReviewOutcomeReceipts:
    """Re-read the whole receipt set before accepting a caller-held inventory."""
    if not isinstance(observed, CompleteLegacyReviewOutcomeReceipts):
        raise ValueError("outcome receipt revalidation requires a complete observed inventory")
    current = read_complete_legacy_review_outcome_receipts(db, observed.repository)
    if isinstance(current, BlockedLegacyReviewOutcomeReceipts):
        return current
    if current.source_digest != observed.source_digest:
        return BlockedLegacyReviewOutcomeReceipts(observed.repository, "outcome receipt sources changed after inventory")
    return current


def _verify_legacy_round_outcome_sources(
    round_source: LegacyReviewRoundSource, receipt_source: LegacyReviewOutcomeReceiptSource,
) -> VerifiedLegacyRoundOutcomeStorage | UnverifiedLegacyRoundOutcomeStorage:
    round_id = dict(round_source.fields)["round_id"]
    row = dict(round_source.fields)
    receipt = dict(receipt_source.fields)
    if row["closure_json"] is None or row["outcome_json"] is None:
        return UnverifiedLegacyRoundOutcomeStorage(round_id, "round outcome is not sealed")
    try:
        facts = decode_workflow(row["closure_json"], ReviewRoundFacts)
        outcome = decode_workflow(row["outcome_json"], ReviewRoundOutcome)
        if (row["plan_json"] != encode_workflow(facts.plan)
                or row["planning_json"] != encode_workflow(facts.planning)
                or row["closure_json"] != encode_workflow(facts)
                or row["outcome_json"] != encode_workflow(outcome)
                or receipt["payload_json"] != encode_workflow((facts, outcome))):
            raise ValueError("round outcome and receipt disagree on canonical stored facts")
        return VerifiedLegacyRoundOutcomeStorage(round_source, receipt_source, facts, outcome)
    except (KeyError, TypeError, ValueError, UnicodeError, CorruptControlStore) as exc:
        return UnverifiedLegacyRoundOutcomeStorage(round_id, _printable(str(exc)))


def verify_legacy_round_outcome_storage(
    db: sqlite3.Connection, observed_rounds: CompleteLegacyReviewRounds,
    observed_receipts: CompleteLegacyReviewOutcomeReceipts, round_id: str,
) -> VerifiedLegacyRoundOutcomeStorage | UnverifiedLegacyRoundOutcomeStorage:
    """Recheck complete source sets and pair one sealed round to its exact receipt."""
    validate_workflow_identifier(round_id, "round outcome storage")
    if observed_rounds.repository != observed_receipts.repository:
        return UnverifiedLegacyRoundOutcomeStorage(round_id, "round and receipt inventories disagree on repository")
    rounds = revalidate_complete_legacy_review_rounds(db, observed_rounds)
    if isinstance(rounds, BlockedLegacyReviewRounds):
        return UnverifiedLegacyRoundOutcomeStorage(round_id, rounds.reason)
    receipts = revalidate_complete_legacy_review_outcome_receipts(db, observed_receipts)
    if isinstance(receipts, BlockedLegacyReviewOutcomeReceipts):
        return UnverifiedLegacyRoundOutcomeStorage(round_id, receipts.reason)
    round_source = next((source for source in rounds.rounds if dict(source.fields)["round_id"] == round_id), None)
    receipt_source = next((source for source in receipts.receipts
                           if dict(source.fields)["namespace"] == "review-round-outcome"
                           and dict(source.fields)["key"] == round_id), None)
    if round_source is None or receipt_source is None:
        return UnverifiedLegacyRoundOutcomeStorage(round_id, "round or outcome receipt is absent")
    return _verify_legacy_round_outcome_sources(round_source, receipt_source)


def verify_complete_legacy_round_outcome_storage(
    db: sqlite3.Connection, observed_rounds: CompleteLegacyReviewRounds,
    observed_receipts: CompleteLegacyReviewOutcomeReceipts,
) -> CompleteLegacyRoundOutcomeStorage | BlockedLegacyRoundOutcomeStorage:
    """Account for every round and receipt after one revalidation of each SQL set."""
    if observed_rounds.repository != observed_receipts.repository:
        return BlockedLegacyRoundOutcomeStorage(observed_rounds.repository, "round and receipt repositories disagree")
    rounds = revalidate_complete_legacy_review_rounds(db, observed_rounds)
    if isinstance(rounds, BlockedLegacyReviewRounds):
        return BlockedLegacyRoundOutcomeStorage(observed_rounds.repository, rounds.reason, rounds.source_rowid)
    receipts = revalidate_complete_legacy_review_outcome_receipts(db, observed_receipts)
    if isinstance(receipts, BlockedLegacyReviewOutcomeReceipts):
        return BlockedLegacyRoundOutcomeStorage(observed_rounds.repository, receipts.reason, receipts.source_rowid)
    receipt_by_identity = {(dict(source.fields)["namespace"], dict(source.fields)["key"]): source
                           for source in receipts.receipts}
    sealed: list[VerifiedLegacyRoundOutcomeStorage] = []
    closed_ids: set[str] = set()
    for source in rounds.rounds:
        row = dict(source.fields)
        round_id = row["round_id"]
        if (row["closure_json"] is None) != (row["outcome_json"] is None):
            return BlockedLegacyRoundOutcomeStorage(rounds.repository, "review round is partially sealed",
                                                    source.source_rowid)
        if row["outcome_json"] is None:
            continue
        closed_ids.add(round_id)
        receipt = receipt_by_identity.get(("review-round-outcome", round_id))
        if receipt is None:
            return BlockedLegacyRoundOutcomeStorage(rounds.repository, "sealed round lacks its outcome receipt",
                                                    source.source_rowid)
        proof = _verify_legacy_round_outcome_sources(source, receipt)
        if isinstance(proof, UnverifiedLegacyRoundOutcomeStorage):
            return BlockedLegacyRoundOutcomeStorage(rounds.repository, proof.reason, source.source_rowid)
        sealed.append(proof)
    for receipt in receipts.receipts:
        values = dict(receipt.fields)
        if values["key"] not in closed_ids or values["reusable_after"] is not None:
            return BlockedLegacyRoundOutcomeStorage(rounds.repository,
                                                    "outcome or productivity receipt lacks permanent sealed round",
                                                    receipt.source_rowid)
        try:
            _parse_stamp(values["claimed_at"])
        except (TypeError, ValueError, AttributeError):
            return BlockedLegacyRoundOutcomeStorage(rounds.repository,
                                                    "outcome receipt has malformed claim time", receipt.source_rowid)
    for proof in sealed:
        round_id = proof.facts.plan.round.round_id
        result = finalize_review_round(proof.facts)
        preparation = receipt_by_identity.get(("review-round-preparation", round_id))
        if isinstance(result, DeferredReviewPreparation):
            if preparation is None or dict(preparation.fields)["payload_json"] != encode_workflow(
                (proof.facts, result.failures)
            ):
                return BlockedLegacyRoundOutcomeStorage(rounds.repository,
                                                        "preparation receipt disagrees with deferred round",
                                                        proof.round_source.source_rowid)
        elif preparation is not None:
            return BlockedLegacyRoundOutcomeStorage(rounds.repository,
                                                    "nondeferred round has a preparation receipt",
                                                    preparation.source_rowid)
        stale = receipt_by_identity.get(("review-round-stale-productivity", round_id))
        if stale is not None and dict(stale.fields)["payload_json"] != encode_workflow((proof.facts, proof.outcome)):
            return BlockedLegacyRoundOutcomeStorage(rounds.repository,
                                                    "stale productivity receipt disagrees with its round",
                                                    stale.source_rowid)
        for namespace, expected in (("review-round-superseded-productivity", str),
                                    ("review-round-ceiling-productivity", CeilingProductivityDecision)):
            source = receipt_by_identity.get((namespace, round_id))
            if source is None:
                continue
            try:
                payload = dict(source.fields)["payload_json"]
                decoded = decode_workflow(payload, tuple)
                if (len(decoded) != 3 or not isinstance(decoded[0], expected)
                        or not isinstance(decoded[1], WorkflowRevision) or not isinstance(decoded[2], str)
                        or decoded[1].fence != proof.facts.plan.round.binding.rejection.fence
                        or encode_workflow(decoded) != payload):
                    raise ValueError("productivity receipt has malformed settlement fields")
                _parse_stamp(decoded[2])
            except (TypeError, ValueError, AttributeError, UnicodeError):
                return BlockedLegacyRoundOutcomeStorage(rounds.repository,
                                                        "productivity receipt has malformed settlement fields",
                                                        source.source_rowid)
    try:
        return CompleteLegacyRoundOutcomeStorage(rounds, receipts, tuple(sealed))
    except ValueError as exc:
        return BlockedLegacyRoundOutcomeStorage(rounds.repository, _printable(str(exc)))


def read_legacy_review_round_page(
    db: sqlite3.Connection, repository: str, *, after_rowid: int | None = None,
) -> LegacyReviewRoundPage:
    """Inventory every round row for a repository without interpreting outcomes."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("review round scan requires an explicit SQL transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("review round scan requires a canonical repository")
    if after_rowid is not None and (type(after_rowid) is not int or not MIN_SQL_ROWID <= after_rowid <= MAX_SQL_ROWID):
        raise ValueError("review round scan requires a valid cursor")
    json_columns = ("plan_json", "planning_json", "closure_json", "outcome_json")
    other_columns = tuple(column for column in LEGACY_REVIEW_ROUND_COLUMNS if column not in json_columns)
    json_sizes = [f"ifnull(length(CAST({column} AS BLOB)),0)" for column in json_columns]
    other_sizes = [f"ifnull(length(CAST({column} AS BLOB)),0)" for column in other_columns]
    predicate = "WHERE repository IN (?,?)" + (" AND rowid>?" if after_rowid is not None else "")
    params = (repository, repository.encode("utf-8")) if after_rowid is None else (
        repository, repository.encode("utf-8"), after_rowid,
    )
    metadata = db.execute(
        f"SELECT rowid AS source_rowid,max({','.join(json_sizes)}) AS largest_json,"
        f"max({','.join(other_sizes)}) AS largest_other,"
        f"{'+'.join(json_sizes + other_sizes)} AS total_bytes "
        f"FROM review_execution_rounds NOT INDEXED {predicate} ORDER BY rowid LIMIT ?",
        (*params, MAX_REVIEW_ROUND_SOURCE_PAGE + 1),
    ).fetchall()
    selected: list[sqlite3.Row] = []
    retained_bytes = 0
    for row in metadata[:MAX_REVIEW_ROUND_SOURCE_PAGE]:
        safe = (row["largest_json"] <= MAX_LEGACY_ROUND_JSON_BYTES
                and row["largest_other"] <= MAX_LEGACY_SQL_TEXT_BYTES)
        if safe and selected and retained_bytes + row["total_bytes"] > MAX_LEGACY_ROUND_PAGE_BYTES:
            break
        selected.append(row)
        if safe:
            retained_bytes += row["total_bytes"]
    safe_ids = [row["source_rowid"] for row in selected if (
        row["largest_json"] <= MAX_LEGACY_ROUND_JSON_BYTES
        and row["largest_other"] <= MAX_LEGACY_SQL_TEXT_BYTES
        and row["total_bytes"] <= MAX_LEGACY_ROUND_PAGE_BYTES
    )]
    values = {}
    if safe_ids:
        ids = ",".join("?" for _ in safe_ids)
        fields = ",".join(
            f"typeof({column}) AS type_{column},CAST({column} AS BLOB) AS raw_{column}"
            for column in LEGACY_REVIEW_ROUND_COLUMNS
        )
        values = {row["source_rowid"]: row for row in db.execute(
            f"SELECT rowid AS source_rowid,{fields} FROM review_execution_rounds "
            f"WHERE repository IN (?,?) AND rowid IN ({ids})",
            (repository, repository.encode("utf-8"), *safe_ids),
        )}
    integer_columns = {"target_number", "execution_epoch", "generation", "sequence"}
    entries: list[LegacyReviewRoundSource | UnreadableLegacyReviewRound] = []
    for row in selected:
        rowid = row["source_rowid"]
        if (row["largest_json"] > MAX_LEGACY_ROUND_JSON_BYTES
                or row["largest_other"] > MAX_LEGACY_SQL_TEXT_BYTES
                or row["total_bytes"] > MAX_LEGACY_ROUND_PAGE_BYTES):
            entries.append(UnreadableLegacyReviewRound(rowid, "review round exceeds its SQL source bound"))
            continue
        source = values.get(rowid)
        if source is None:
            entries.append(UnreadableLegacyReviewRound(rowid, "review round changed during its read"))
            continue
        try:
            fields = tuple((column, (
                _sql_integer(source[f"type_{column}"], source[f"raw_{column}"])
                if column in integer_columns else _sql_text(source[f"type_{column}"], source[f"raw_{column}"])
            )) for column in LEGACY_REVIEW_ROUND_COLUMNS)
            entries.append(LegacyReviewRoundSource(rowid, fields))
        except (TypeError, ValueError, UnicodeError):
            entries.append(UnreadableLegacyReviewRound(rowid, "review round has malformed fields"))
    return LegacyReviewRoundPage(
        tuple(entries), selected[-1]["source_rowid"] if selected else after_rowid,
        len(metadata) > len(selected),
    )


def read_complete_legacy_review_rounds(
    db: sqlite3.Connection, repository: str,
) -> CompleteLegacyReviewRounds | BlockedLegacyReviewRounds:
    """Scan every round source once; a partial page is never import evidence."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("complete review round scan requires an explicit SQL transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("complete review round scan requires a canonical repository")
    if db.execute(
        "SELECT rowid FROM review_execution_rounds NOT INDEXED ORDER BY rowid LIMIT 1 OFFSET ?",
        (MAX_LEGACY_ROUND_SCAN_ROWS,),
    ).fetchone() is not None:
        return BlockedLegacyReviewRounds(repository, "review round table exceeds its total scan bound")
    rounds: list[LegacyReviewRoundSource] = []
    source_bytes = 0
    cursor = None
    while True:
        page = read_legacy_review_round_page(db, repository, after_rowid=cursor)
        for entry in page.entries:
            if isinstance(entry, UnreadableLegacyReviewRound):
                return BlockedLegacyReviewRounds(repository, entry.reason, entry.source_rowid)
            source_bytes += entry.source_bytes
            if len(rounds) >= MAX_LEGACY_ROUND_INVENTORY_ROWS or source_bytes > MAX_LEGACY_ROUND_INVENTORY_BYTES:
                return BlockedLegacyReviewRounds(repository, "review round inventory exceeds its total bound",
                                                 entry.source_rowid)
            rounds.append(entry)
        if not page.has_more:
            break
        cursor = page.next_rowid
    try:
        return CompleteLegacyReviewRounds(repository, tuple(rounds))
    except ValueError as exc:
        return BlockedLegacyReviewRounds(repository, str(exc)[:512])


def revalidate_complete_legacy_review_rounds(
    db: sqlite3.Connection, observed: CompleteLegacyReviewRounds,
) -> CompleteLegacyReviewRounds | BlockedLegacyReviewRounds:
    """Re-read the whole repository before accepting a caller-held inventory."""
    if not isinstance(observed, CompleteLegacyReviewRounds):
        raise ValueError("round revalidation requires a complete observed inventory")
    current = read_complete_legacy_review_rounds(db, observed.repository)
    if isinstance(current, BlockedLegacyReviewRounds):
        return current
    if current.source_digest != observed.source_digest:
        return BlockedLegacyReviewRounds(observed.repository, "review round sources changed after inventory")
    return current


def read_legacy_review_owner_lineage(
    db: sqlite3.Connection, repository: str, current_owner: str,
    *, configured_prior_owners: tuple[str, ...] = (),
) -> LegacyReviewOwnerLineage | UnreadableLegacyReviewOwnerLineage:
    """Resolve a bounded review-owner closure without adopting or rewriting it.

    The caller supplies exact config-authorized predecessors; retained
    retirements extend that set. Every run scan must use the same SQL snapshot.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("owner lineage requires an explicit SQL transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("owner lineage requires a canonical repository")
    validate_workflow_identifier(current_owner, "current review owner")
    if (not isinstance(configured_prior_owners, tuple)
            or len(configured_prior_owners) >= MAX_LEGACY_REVIEW_OWNERS):
        raise ValueError("owner lineage requires bounded configured predecessors")
    for owner in (current_owner, *configured_prior_owners):
        validate_workflow_identifier(owner, "configured review owner")
        try:
            owner.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError("review owner must have a UTF-8 identity") from exc
    implicit_legacy_owner = f"{repository}:review"
    if current_owner != implicit_legacy_owner and not current_owner.startswith(implicit_legacy_owner + ":"):
        raise ValueError("current review owner does not match its repository and role")
    configured = tuple(dict.fromkeys(
        owner for owner in configured_prior_owners if owner not in {current_owner, implicit_legacy_owner}
    ))
    owners = list(dict.fromkeys((current_owner, implicit_legacy_owner, *configured)))
    if len(owners) > MAX_LEGACY_REVIEW_OWNERS:
        raise ValueError("owner lineage has too many configured predecessors")

    current_retirement = db.execute(
        "SELECT rowid FROM retired_owners WHERE owner_id IN (?,?)",
        (current_owner, current_owner.encode("utf-8")),
    ).fetchone()
    if current_retirement is not None:
        return UnreadableLegacyReviewOwnerLineage("current review owner is retired", current_retirement["rowid"])

    seen = set(owners)
    links: list[LegacyReviewOwnerLink] = []
    processed: set[str] = set()
    frontier = owners.copy()
    sizes = ",".join(f"ifnull(length(CAST({column} AS BLOB)),0)" for column in _RETIRED_OWNER_COLUMNS)
    while frontier:
        frontier = [owner for owner in frontier if owner not in processed]
        if not frontier:
            break
        processed.update(frontier)
        lookup = (*frontier, *(owner.encode("utf-8") for owner in frontier))
        placeholders = ",".join("?" for _ in lookup)
        metadata = db.execute(
            f"SELECT rowid AS source_rowid,max({sizes}) AS largest_value FROM retired_owners "
            f"WHERE successor_owner_id IN ({placeholders}) ORDER BY rowid LIMIT ?",
            (*lookup, MAX_LEGACY_REVIEW_OWNERS + 1),
        ).fetchall()
        if len(metadata) > MAX_LEGACY_REVIEW_OWNERS:
            return UnreadableLegacyReviewOwnerLineage("retired review owner frontier exceeds the bound")
        oversized = next((row for row in metadata if row["largest_value"] > MAX_LEGACY_SQL_TEXT_BYTES), None)
        if oversized is not None:
            return UnreadableLegacyReviewOwnerLineage(
                "retired review owner field exceeds the SQL text bound", oversized["source_rowid"],
            )
        if not metadata:
            break
        rowids = [row["source_rowid"] for row in metadata]
        ids = ",".join("?" for _ in rowids)
        fields = ",".join(
            f"typeof({column}) AS type_{column},CAST({column} AS BLOB) AS raw_{column}"
            for column in _RETIRED_OWNER_COLUMNS
        )
        values = {row["source_rowid"]: row for row in db.execute(
            f"SELECT rowid AS source_rowid,{fields} FROM retired_owners WHERE rowid IN ({ids})", rowids,
        )}
        next_frontier: list[str] = []
        for meta in metadata:
            rowid = meta["source_rowid"]
            source = values.get(rowid)
            if source is None:
                return UnreadableLegacyReviewOwnerLineage("retired review owner changed during its read", rowid)
            try:
                values_by_name = {
                    column: _sql_text(source[f"type_{column}"], source[f"raw_{column}"])
                    for column in _RETIRED_OWNER_COLUMNS
                }
                link = LegacyReviewOwnerLink(rowid, **values_by_name)
            except (TypeError, ValueError, UnicodeError):
                return UnreadableLegacyReviewOwnerLineage("retired review owner has malformed fields", rowid)
            if (link.repository != repository or link.role != "review"
                    or link.successor_owner_id not in processed):
                return UnreadableLegacyReviewOwnerLineage("retired review owner conflicts with the lineage", rowid)
            links.append(link)
            if link.owner_id not in seen:
                seen.add(link.owner_id)
                owners.append(link.owner_id)
            next_frontier.append(link.owner_id)
            if len(owners) > MAX_LEGACY_REVIEW_OWNERS:
                return UnreadableLegacyReviewOwnerLineage("review owner lineage exceeds the bound", rowid)
        frontier = next_frontier
    owner_lookup = (*owners, *(owner.encode("utf-8") for owner in owners))
    owner_marks = ",".join("?" for _ in owner_lookup)
    outgoing = db.execute(
        f"SELECT rowid FROM retired_owners WHERE owner_id IN ({owner_marks}) LIMIT ?",
        (*owner_lookup, MAX_LEGACY_REVIEW_OWNERS + 1),
    ).fetchall()
    if (len(outgoing) > MAX_LEGACY_REVIEW_OWNERS
            or {row["rowid"] for row in outgoing} != {link.source_rowid for link in links}):
        return UnreadableLegacyReviewOwnerLineage("review owner has an unjoined retirement receipt")
    try:
        return LegacyReviewOwnerLineage(repository, current_owner, configured,
                                        tuple(owners), tuple(links))
    except ValueError:
        return UnreadableLegacyReviewOwnerLineage("review owner lineage is contradictory")


def _read_recording_source(
    db: sqlite3.Connection, namespace: str, repository: str, key: str,
) -> tuple[tuple[tuple[str, str | None], ...], str]:
    sizes = ",".join(f"ifnull(length(CAST({column} AS BLOB)),0)" for column in _DEDUPE_SOURCE_COLUMNS)
    metadata = db.execute(
        f"SELECT rowid AS source_rowid,max({sizes}) AS largest_value FROM dedupe_keys "
        "WHERE namespace=? AND scope=? AND key=?", (namespace, repository, key),
    ).fetchone()
    if metadata is None:
        raise ValueError(f"{namespace} source is absent")
    if metadata["largest_value"] > MAX_LEGACY_RECORDING_BYTES:
        raise ValueError(f"{namespace} source exceeds its byte bound")
    columns = ",".join(
        f"typeof({column}) AS type_{column},CAST({column} AS BLOB) AS raw_{column}"
        for column in _DEDUPE_SOURCE_COLUMNS
    )
    source = db.execute(
        f"SELECT {columns} FROM dedupe_keys WHERE rowid=? AND namespace=? AND scope=? AND key=?",
        (metadata["source_rowid"], namespace, repository, key),
    ).fetchone()
    if source is None:
        raise ValueError(f"{namespace} source changed during its read")
    fields = tuple(
        (column, _sql_text(source[f"type_{column}"], source[f"raw_{column}"]))
        for column in _DEDUPE_SOURCE_COLUMNS
    )
    values = dict(fields)
    if values["payload_json"] is None or values["claimed_at"] is None:
        raise ValueError(f"{namespace} source lacks its payload or claim")
    digest = hashlib.sha256(json.dumps(fields, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    return fields, digest


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"accepted review transition contains {value}")


def _verified_review_stage_sources(db: sqlite3.Connection, repository: str, plan: ReviewRoundPlan) -> str:
    prefix = review_stage_receipt_prefix(plan.round.round_id)
    rows = db.execute(
        "SELECT rowid AS source_rowid,typeof(key) AS key_type,length(CAST(key AS BLOB)) AS key_bytes,"
        "ifnull(length(CAST(payload_json AS BLOB)),0) AS payload_bytes "
        "FROM dedupe_keys WHERE namespace='review-round-stage' AND scope=? "
        "AND key>=? AND key<? ORDER BY key LIMIT ?",
        (repository, prefix, prefix[:-1] + ";", MAX_REVIEW_STAGES + 1),
    ).fetchall()
    if len(rows) != len(plan.stages):
        raise ValueError("review stages disagree with their bounded receipt index")
    planned = {stage.stage_id: stage for stage in plan.stages}
    digests: list[tuple[str, str]] = []
    for row in rows:
        if (row["key_type"] != "text" or row["key_bytes"] > 512
                or row["payload_bytes"] > MAX_LEGACY_SQL_TEXT_BYTES):
            raise ValueError("review stage receipt exceeds its SQL source bound")
        key_row = db.execute("SELECT key FROM dedupe_keys WHERE rowid=?", (row["source_rowid"],)).fetchone()
        if key_row is None:
            raise ValueError("review stage receipt changed during its read")
        stage_key = key_row["key"]
        fields, digest = _read_recording_source(db, "review-round-stage", repository, stage_key)
        recorded = decode_workflow(dict(fields)["payload_json"], tuple)
        if (len(recorded) != 2 or recorded[0] != plan.round.round_id
                or not isinstance(recorded[1], ReviewStagePlan)
                or stage_key != review_stage_receipt_key(plan.round.round_id, recorded[1].stage_id)
                or planned.get(recorded[1].stage_id) != recorded[1]):
            raise ValueError("review stage receipt disagrees with its round")
        digests.append((stage_key, digest))
    return hashlib.sha256(json.dumps(digests, separators=(",", ":")).encode()).hexdigest()


def _recheck_submission_source(db: sqlite3.Connection, source: LegacyReviewSubmissionSource) -> None:
    command_id = dict(source.command_fields)["command_id"]
    for table, columns, required in (
        ("lifecycle_commands", LEGACY_SUBMISSION_COMMAND_COLUMNS, True),
        ("effect_receipts", LEGACY_SUBMISSION_RECEIPT_COLUMNS, source.receipt_fields is not None),
    ):
        sizes = ",".join(f"ifnull(length(CAST({column} AS BLOB)),0)" for column in columns)
        metadata = db.execute(
            f"SELECT max({sizes}) AS largest_value FROM {table} WHERE command_id=?", (command_id,),
        ).fetchone()
        if (metadata is None) != (not required):
            raise ValueError("submission command or effect changed after inventory")
        if metadata is not None and metadata["largest_value"] > MAX_LEGACY_SQL_TEXT_BYTES:
            raise ValueError("submission command or effect exceeds its SQL source bound")
    command_row = db.execute(
        "SELECT rowid AS source_rowid,* FROM lifecycle_commands WHERE command_id=?", (command_id,),
    ).fetchone()
    effect_row = db.execute("SELECT * FROM effect_receipts WHERE command_id=?", (command_id,)).fetchone()
    if (command_row is None or command_row["source_rowid"] != source.source_rowid
            or any(command_row[name] != value for name, value in source.command_fields)
            or (effect_row is None) != (source.receipt_fields is None)
            or (effect_row is not None and source.receipt_fields is not None
                and any(effect_row[name] != value for name, value in source.receipt_fields))):
        raise ValueError("submission command or effect changed after inventory")


def read_legacy_recorded_review(
    db: sqlite3.Connection, source: LegacyReviewSubmissionSource,
    lineage: LegacyReviewOwnerLineage,
) -> VerifiedLegacyRecordedReview | UnverifiedLegacyRecordedReview:
    """Prove one plain recorded baseline from its accepted SQL sources.

    This does not certify a productive execution round or reset. Escalated
    verdicts need a separate hold proof; missing recording remains unresolved.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("recorded review proof requires an explicit SQL transaction")
    if not isinstance(source, LegacyReviewSubmissionSource) or not isinstance(lineage, LegacyReviewOwnerLineage):
        raise ValueError("recorded review proof requires typed command and lineage sources")
    command = dict(source.command_fields)
    command_id = command["command_id"]
    if command["repository"] != lineage.repository:
        return UnverifiedLegacyRecordedReview(command_id, "submission belongs to another repository")
    if source.receipt_fields is None:
        return UnverifiedLegacyRecordedReview(command_id, "submission effect receipt is absent")
    effect = dict(source.receipt_fields)
    try:
        _recheck_submission_source(db, source)
        if command["state"] != "succeeded" or effect["status"] != "succeeded":
            raise ValueError("submission lacks a successful effect")
        if (command["attempts"] < 1 or command["workflow_binding_json"] is None
                or command["payload_digest"] != _encoded_payload_digest(
                    command["payload_json"], command["workflow_binding_json"],
                )):
            raise ValueError("submission command lacks its bound payload or paid attempt")
        receipt = EffectReceipt(
            CommandId(command_id), effect["executor_identity"],
            _parse_stamp(effect["started_at"]), _parse_stamp(effect["completed_at"]),
            EffectStatus(effect["status"]), effect["remote_object_id"], effect["remote_revision"], effect["error"],
        )
        if (command["attempt_owner"] != receipt.executor_identity
                or effect["receipt_digest"] != _receipt_digest(receipt)):
            raise ValueError("submission effect digest or attempt owner disagrees")
        duplicate_review = db.execute(
            "SELECT e.command_id FROM effect_receipts e JOIN lifecycle_commands c USING(command_id) "
            "WHERE e.remote_object_id=? AND c.kind='submit_review' AND c.repository=? LIMIT 2",
            (receipt.remote_object_id, lineage.repository),
        ).fetchall()
        if len(duplicate_review) != 1 or duplicate_review[0]["command_id"] != command_id:
            raise ValueError("submission review identity is shared by another command")
        bound = load_bound_review_submission(db, CommandId(command_id))
        if not isinstance(bound, ReviewSubmissionReceipt) or bound.escalated:
            raise ValueError("submission is not a plain bound verdict; hold provenance remains required")
        recorded_fields, recorded_digest = _read_recording_source(
            db, "review-recorded-submission", lineage.repository, command_id,
        )
        recorded = decode_workflow(dict(recorded_fields)["payload_json"], WorkflowRevision)
        if recorded.version == 0:
            raise ValueError("recorded review has no predecessor revision")
        expected = WorkflowRevision(recorded.fence, recorded.version - 1)
        transition_key = hashlib.sha256(encode_workflow(expected).encode()).hexdigest()
        transition_fields, transition_digest = _read_recording_source(
            db, "workflow-transition", lineage.repository, transition_key,
        )
        for namespace, fields in (("review-recorded-submission", recorded_fields),
                                  ("workflow-transition", transition_fields)):
            receipt_fields = dict(fields)
            if receipt_fields["reusable_after"] is not None:
                raise ValueError(f"{namespace} baseline receipt is reclaimable")
            _parse_stamp(receipt_fields["claimed_at"])
        accepted = json.loads(dict(transition_fields)["payload_json"],
                              object_pairs_hook=_unique_receipt_object, parse_constant=_reject_nonfinite)
        if (not isinstance(accepted, dict)
                or set(accepted) != {"mutation", "expected", "snapshot", "audit", "commands"}
                or accepted["mutation"] != WorkflowMutation.RECORD_REVIEW.value
                or accepted["commands"] != []):
            raise ValueError("accepted transition is not a plain recorded review")
        accepted_expected = decode_workflow(accepted["expected"], WorkflowRevision)
        after = decode_workflow(accepted["snapshot"], ReviewWorkflowSnapshot)
        audit = decode_workflow(accepted["audit"], ObservedTransition)
        if accepted_expected != expected:
            raise ValueError("accepted transition expected revision changed")
        if (after.revision != recorded or after.binding != bound.submission.binding
                or (not isinstance(after.execution, ExecutionNotActivated)
                    and (not isinstance(after.execution, ActivatedReviewExecution)
                         or after.execution.binding.rejection != bound.submission.binding))):
            raise ValueError("accepted transition is not a bound review baseline")
        return VerifiedLegacyRecordedReview(source, lineage, bound, expected, recorded,
                                            after.episode.continuation, audit,
                                            recorded_digest, transition_digest,
                                            after.execution.binding if isinstance(after.execution, ActivatedReviewExecution)
                                            else None)
    except (KeyError, TypeError, ValueError, UnicodeError, sqlite3.Error,
            WorkflowAuthorityError, CorruptControlStore) as exc:
        return UnverifiedLegacyRecordedReview(command_id, _printable(str(exc)))


def read_review_submission_round_link(
    db: sqlite3.Connection, source: LegacyReviewSubmissionSource,
    lineage: LegacyReviewOwnerLineage,
) -> VerifiedReviewSubmissionRoundLink | UnverifiedReviewSubmissionRoundLink:
    """Join a registered review command to its exact round plan in one SQL snapshot.

    Registration proves planning provenance only. A confirmed productive
    outcome still needs a sealed round, effect and recorded baseline receipt.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("review round link requires an explicit SQL transaction")
    if not isinstance(source, LegacyReviewSubmissionSource) or not isinstance(lineage, LegacyReviewOwnerLineage):
        raise ValueError("review round link requires typed command and lineage sources")
    command = dict(source.command_fields)
    command_id = command["command_id"]
    if command["repository"] != lineage.repository:
        return UnverifiedReviewSubmissionRoundLink(command_id, "submission belongs to another repository")
    try:
        current_lineage = read_legacy_review_owner_lineage(
            db, lineage.repository, lineage.current_owner,
            configured_prior_owners=lineage.configured_prior_owners,
        )
        if current_lineage != lineage:
            raise ValueError("review owner lineage changed after inventory")
        _recheck_submission_source(db, source)
        if source.receipt_fields is not None:
            effect = dict(source.receipt_fields)
            receipt = EffectReceipt(
                CommandId(command_id), effect["executor_identity"],
                _parse_stamp(effect["started_at"]), _parse_stamp(effect["completed_at"]),
                EffectStatus(effect["status"]), effect["remote_object_id"], effect["remote_revision"], effect["error"],
            )
            if (command["attempts"] < 1 or command["attempt_owner"] != receipt.executor_identity
                    or effect["receipt_digest"] != _receipt_digest(receipt)):
                raise ValueError("review effect receipt lacks its paid attempt or digest")
        registration_fields, registration_digest = _read_recording_source(
            db, "review-round-submission", lineage.repository, command_id,
        )
        registration = decode_workflow(dict(registration_fields)["payload_json"], ReviewSubmissionRegistration)
        if (registration.submission.command_id != command_id
                or registration.owner_id != command["executor_identity"]
                or registration.owner_id not in lineage.owners
                or registration.command_digest != command["payload_digest"]
                or registration.idempotency_key != command["idempotency_key"]
                or registration.config_version != command["config_version"]
                or command["kind"] != "submit_review"):
            raise ValueError("review registration disagrees with its exact command")
        index_fields, index_digest = _read_recording_source(
            db, "review-round-submission-index", lineage.repository,
            review_submission_receipt_key(registration.round.round_id, command_id),
        )
        recorded = dict(registration_fields)
        indexed = dict(index_fields)
        if (indexed["payload_json"] != recorded["payload_json"]
                or indexed["claimed_at"] != recorded["claimed_at"]
                or indexed["reusable_after"] is not None or recorded["reusable_after"] is not None):
            raise ValueError("review registration and round index disagree")
        round_id = registration.round.round_id
        preflight_fields, preflight_digest = _read_recording_source(
            db, "review-round-preflight", lineage.repository, round_id,
        )
        preflight = decode_workflow(dict(preflight_fields)["payload_json"], tuple)
        if (len(preflight) != 3 or not isinstance(preflight[0], ReviewPreflightSelection)
                or not isinstance(preflight[1], str) or preflight[1] != registration.owner_id
                or not isinstance(preflight[2], ReviewRoundPlan)
                or preflight[0].request_id != round_id
                or preflight[0].claim_occurrence_id != registration.claim_occurrence_id
                or preflight[0].binding != registration.round.binding
                or preflight[2].round != registration.round):
            raise ValueError("review registration disagrees with its preflight")
        sizes = ",".join(f"ifnull(length(CAST({column} AS BLOB)),0)" for column in (
            "round_id", "repository", "target_kind", "target_number", "execution_epoch",
            "execution_episode", "generation", "sequence", "claim_occurrence_id", "plan_json",
        ))
        metadata = db.execute(
            f"SELECT max({sizes}) AS largest_value FROM review_execution_rounds WHERE round_id=?", (round_id,),
        ).fetchone()
        if metadata is None or metadata["largest_value"] is None:
            raise ValueError("registered review round is absent")
        if metadata["largest_value"] > MAX_LEGACY_RECORDING_BYTES:
            raise ValueError("registered review round exceeds its byte bound")
        row = db.execute(
            "SELECT round_id,repository,target_kind,target_number,execution_epoch,execution_episode,"
            "generation,sequence,claim_occurrence_id,plan_json "
            "FROM review_execution_rounds WHERE round_id=?", (round_id,),
        ).fetchone()
        if row is None:
            raise ValueError("registered review round changed during its read")
        fields = tuple((name, row[name]) for name in row.keys())
        if any(type(value) is not (int if name in {"target_number", "execution_epoch", "generation", "sequence"}
                                   else str) for name, value in fields):
            raise ValueError("registered review round has malformed SQL fields")
        plan = decode_workflow(row["plan_json"], ReviewRoundPlan)
        key = plan.round.binding.rejection.fence.key
        if (plan.round != registration.round or row["repository"] != lineage.repository
                or row["target_kind"] != "pr" or row["target_number"] != key.target.number
                or row["execution_epoch"] != plan.round.binding.execution_epoch
                or row["execution_episode"] != plan.round.binding.execution_episode
                or row["generation"] != plan.round.binding.rejection.fence.generation
                or row["sequence"] != plan.round.sequence
                or row["claim_occurrence_id"] != registration.claim_occurrence_id):
            raise ValueError("registered review round index disagrees with its plan")
        if (replace(preflight[2], stages=plan.stages, submissions=plan.submissions) != plan
                or registration.submission not in plan.submissions):
            raise ValueError("registered review submission disagrees with its evolved plan")
        if registration.submission.kind is ReviewSubmissionKind.PAID and not plan.stages:
            raise ValueError("paid review submission lacks a planned stage")
        if registration.submission.kind is ReviewSubmissionKind.COPIED and plan.stages:
            effect = dict(source.receipt_fields) if source.receipt_fields is not None else None
            if (command["state"] != "permanent_failure" or command["attempts"] < 1
                    or effect is None or effect["status"] != "permanent_failure"
                    or effect["remote_object_id"] is not None):
                raise ValueError("copied review with later stages lacks its definitive refusal")
        stage_digest = _verified_review_stage_sources(db, lineage.repository, plan)
        if (command["expected_revision"] != plan.head_sha
                or command["workflow_binding_json"] != encode_workflow(registration.round.binding.rejection)
                or command["payload_digest"] != _encoded_payload_digest(
                    command["payload_json"], command["workflow_binding_json"],
                )):
            raise ValueError("registered review command disagrees with its plan")
        payload = json.loads(command["payload_json"], object_pairs_hook=_unique_receipt_object,
                             parse_constant=_reject_nonfinite)
        if (not isinstance(payload, dict) or payload.get("head") != plan.head_sha
                or payload.get("pr") != key.target.number):
            raise ValueError("registered review command payload disagrees with its plan")
        round_digest = hashlib.sha256(json.dumps(fields, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        return VerifiedReviewSubmissionRoundLink(source, lineage, registration.round,
                                                 registration_digest, index_digest,
                                                 preflight_digest, stage_digest, round_digest)
    except (KeyError, TypeError, ValueError, UnicodeError, sqlite3.Error,
            WorkflowAuthorityError, CorruptControlStore) as exc:
        return UnverifiedReviewSubmissionRoundLink(command_id, _printable(str(exc)))


def verify_legacy_plain_productive_round(
    db: sqlite3.Connection, observed_storage: CompleteLegacyRoundOutcomeStorage,
    source: LegacyReviewSubmissionSource, lineage: LegacyReviewOwnerLineage,
) -> VerifiedLegacyPlainProductiveRound | UnverifiedLegacyPlainProductiveRound:
    """Join one confirmed plain verdict, sealed outcome and recorded baseline."""
    if (not isinstance(observed_storage, CompleteLegacyRoundOutcomeStorage)
            or not isinstance(source, LegacyReviewSubmissionSource)
            or not isinstance(lineage, LegacyReviewOwnerLineage)):
        raise ValueError("plain productive round proof requires typed source observations")
    command_id = dict(source.command_fields)["command_id"]
    if observed_storage.repository != lineage.repository:
        return UnverifiedLegacyPlainProductiveRound(command_id, "productive round repositories disagree")
    current = verify_complete_legacy_round_outcome_storage(db, observed_storage.rounds, observed_storage.receipts)
    if isinstance(current, BlockedLegacyRoundOutcomeStorage):
        return UnverifiedLegacyPlainProductiveRound(command_id, current.reason)
    link = read_review_submission_round_link(db, source, lineage)
    if isinstance(link, UnverifiedReviewSubmissionRoundLink):
        return UnverifiedLegacyPlainProductiveRound(command_id, link.reason)
    storage = next((entry for entry in current.sealed if entry.outcome.round == link.round), None)
    if storage is None:
        return UnverifiedLegacyPlainProductiveRound(command_id, "linked review round lacks a sealed outcome")
    baseline = read_legacy_recorded_review(db, source, lineage)
    if isinstance(baseline, UnverifiedLegacyRecordedReview):
        return UnverifiedLegacyPlainProductiveRound(command_id, baseline.reason)
    try:
        effect = dict(source.receipt_fields) if source.receipt_fields is not None else None
        if effect is None:
            raise ValueError("productive round lacks its effect receipt")
        return VerifiedLegacyPlainProductiveRound(
            current, storage, link, baseline, _effect_receipt_id(command_id, effect["receipt_digest"]),
        )
    except ValueError as exc:
        return UnverifiedLegacyPlainProductiveRound(command_id, _printable(str(exc)))


@dataclass(frozen=True)
class VerifiedLegacySubmissionRegistrationStorage:
    """One command, registration, index and planned round agree on storage identity."""

    command_source: LegacyReviewSubmissionSource
    registration_source: LegacyReviewSubmissionReceiptSource
    index_source: LegacyReviewSubmissionReceiptSource
    round_source: LegacyReviewRoundSource
    plan: ReviewRoundPlan
    registration: ReviewSubmissionRegistration

    def __post_init__(self) -> None:
        if (not isinstance(self.command_source, LegacyReviewSubmissionSource)
                or not isinstance(self.registration_source, LegacyReviewSubmissionReceiptSource)
                or not isinstance(self.index_source, LegacyReviewSubmissionReceiptSource)
                or not isinstance(self.round_source, LegacyReviewRoundSource)
                or not isinstance(self.plan, ReviewRoundPlan)
                or not isinstance(self.registration, ReviewSubmissionRegistration)):
            raise ValueError("registered submission storage requires typed sources")
        command = dict(self.command_source.command_fields)
        receipt = dict(self.registration_source.fields)
        index = dict(self.index_source.fields)
        round_row = dict(self.round_source.fields)
        try:
            payload = json.loads(command["payload_json"], object_pairs_hook=_unique_receipt_object,
                                 parse_constant=_reject_nonfinite)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("registered submission has malformed command payload") from exc
        if (receipt["namespace"] != "review-round-submission"
                or index["namespace"] != "review-round-submission-index"
                or receipt["scope"] != command["repository"] or index["scope"] != command["repository"]
                or receipt["key"] != command["command_id"]
                or index["key"] != review_submission_receipt_key(
                    self.registration.round.round_id, command["command_id"],
                )
                or receipt["payload_json"] != encode_workflow(self.registration)
                or receipt["payload_json"] != index["payload_json"]
                or receipt["claimed_at"] != index["claimed_at"]
                or receipt["reusable_after"] is not None or index["reusable_after"] is not None
                or self.registration.submission.command_id != command["command_id"]
                or self.registration.round != self.plan.round
                or self.registration.submission not in self.plan.submissions
                or round_row["round_id"] != self.plan.round.round_id
                or round_row["claim_occurrence_id"] != self.registration.claim_occurrence_id
                or self.registration.owner_id != command["executor_identity"]
                or self.registration.command_digest != command["payload_digest"]
                or self.registration.idempotency_key != command["idempotency_key"]
                or self.registration.config_version != command["config_version"]
                or command["target"] != f"pr:{self.plan.round.binding.rejection.fence.key.target.number}"
                or command["expected_revision"] != self.plan.head_sha
                or command["workflow_binding_json"] != encode_workflow(self.plan.round.binding.rejection)
                or command["payload_digest"] != _encoded_payload_digest(
                    command["payload_json"], command["workflow_binding_json"],
                )
                or not isinstance(payload, dict)
                or payload.get("head") != self.plan.head_sha
                or type(payload.get("pr")) is not int
                or payload.get("pr") != self.plan.round.binding.rejection.fence.key.target.number
                or _stored_review_round_plan(self.round_source) != self.plan):
            raise ValueError("registered submission storage has contradictory identities")
        _parse_stamp(receipt["claimed_at"])

    @property
    def source_digest(self) -> str:
        parts = (self.command_source.source_digest, self.registration_source.source_digest,
                 self.index_source.source_digest, self.round_source.source_digest)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class CompleteLegacySubmissionSettlementStorage:
    """Every inventoried command and settlement receipt is accounted for as storage."""

    commands: CompleteLegacyReviewSubmissions
    receipts: CompleteLegacyReviewSubmissionReceipts
    round_storage: CompleteLegacyRoundOutcomeStorage
    round_plans: tuple[tuple[LegacyReviewRoundSource, ReviewRoundPlan], ...]
    registered: tuple[VerifiedLegacySubmissionRegistrationStorage, ...]
    unregistered: tuple[LegacyReviewSubmissionSource, ...]
    recorded_markers: tuple[LegacyReviewSubmissionReceiptSource, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.commands, CompleteLegacyReviewSubmissions)
                or not isinstance(self.receipts, CompleteLegacyReviewSubmissionReceipts)
                or not isinstance(self.round_storage, CompleteLegacyRoundOutcomeStorage)
                or not isinstance(self.round_plans, tuple) or not isinstance(self.registered, tuple)
                or not isinstance(self.unregistered, tuple) or not isinstance(self.recorded_markers, tuple)
                or len({self.commands.repository, self.receipts.repository, self.round_storage.repository}) != 1):
            raise ValueError("submission settlement storage requires matching complete inventories")
        command_by_id = {dict(source.command_fields)["command_id"]: source for source in self.commands.submissions}
        receipt_by_key = {(dict(source.fields)["namespace"], dict(source.fields)["key"]): source
                          for source in self.receipts.receipts}
        round_by_id = {dict(source.fields)["round_id"]: source for source in self.round_storage.rounds.rounds}
        sealed_by_id = {entry.outcome.round.round_id: entry for entry in self.round_storage.sealed}
        plan_by_id: dict[str, ReviewRoundPlan] = {}
        for entry in self.round_plans:
            if (not isinstance(entry, tuple) or len(entry) != 2
                    or not isinstance(entry[0], LegacyReviewRoundSource)
                    or not isinstance(entry[1], ReviewRoundPlan)):
                raise ValueError("settlement storage requires typed round plans")
            source, plan = entry
            round_id = plan.round.round_id
            if (round_id in plan_by_id or round_by_id.get(round_id) != source
                    or _stored_review_round_plan(source) != plan
                    or (round_id in sealed_by_id and sealed_by_id[round_id].facts.plan != plan)):
                raise ValueError("settlement storage has an unaccounted round plan")
            plan_by_id[round_id] = plan
        if set(plan_by_id) != set(round_by_id):
            raise ValueError("settlement storage leaves a round plan unaccounted for")
        registered_by_id: dict[str, VerifiedLegacySubmissionRegistrationStorage] = {}
        for proof in self.registered:
            if not isinstance(proof, VerifiedLegacySubmissionRegistrationStorage):
                raise ValueError("settlement storage requires typed registration proofs")
            command_id = dict(proof.command_source.command_fields)["command_id"]
            round_id = proof.plan.round.round_id
            index = dict(proof.index_source.fields)
            if (command_id in registered_by_id or command_by_id.get(command_id) != proof.command_source
                    or round_by_id.get(round_id) != proof.round_source or plan_by_id.get(round_id) != proof.plan
                    or receipt_by_key.get(("review-round-submission", command_id)) != proof.registration_source
                    or receipt_by_key.get((index["namespace"], index["key"])) != proof.index_source):
                raise ValueError("settlement storage has an unaccounted registration")
            registered_by_id[command_id] = proof
        unregistered_ids = [dict(source.command_fields)["command_id"] for source in self.unregistered
                            if isinstance(source, LegacyReviewSubmissionSource)]
        if (len(unregistered_ids) != len(self.unregistered)
                or len(set(unregistered_ids)) != len(unregistered_ids)
                or any(command_by_id.get(command_id) != source
                       for command_id, source in zip(unregistered_ids, self.unregistered, strict=True))
                or set(registered_by_id) & set(unregistered_ids)
                or set(registered_by_id) | set(unregistered_ids) != set(command_by_id)):
            raise ValueError("settlement storage leaves a command unaccounted for")
        planned_ids = [submission.command_id for plan in plan_by_id.values() for submission in plan.submissions]
        if len(planned_ids) != len(set(planned_ids)) or set(planned_ids) != set(registered_by_id):
            raise ValueError("settlement storage leaves a planned submission unaccounted for")
        marker_keys = set()
        for marker in self.recorded_markers:
            if not isinstance(marker, LegacyReviewSubmissionReceiptSource):
                raise ValueError("settlement storage requires typed recorded markers")
            values = dict(marker.fields)
            if (values["namespace"] != "review-recorded-submission"
                    or receipt_by_key.get((values["namespace"], values["key"])) != marker
                    or values["key"] not in command_by_id):
                raise ValueError("settlement storage has an orphaned recorded marker")
            marker_keys.add(values["key"])
        registration_keys = {("review-round-submission", command_id) for command_id in registered_by_id}
        index_keys = {(dict(proof.index_source.fields)["namespace"], dict(proof.index_source.fields)["key"])
                      for proof in self.registered}
        marker_receipt_keys = {("review-recorded-submission", command_id) for command_id in marker_keys}
        if len(index_keys) != len(self.registered) or set(receipt_by_key) != registration_keys | index_keys | marker_receipt_keys:
            raise ValueError("settlement storage leaves a receipt unaccounted for")

    @property
    def repository(self) -> str:
        return self.commands.repository

    @property
    def source_digest(self) -> str:
        parts = (self.commands.source_digest, self.receipts.source_digest, self.round_storage.source_digest)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def _stored_review_round_plan(source: LegacyReviewRoundSource) -> ReviewRoundPlan:
    row = dict(source.fields)
    plan = decode_workflow(row["plan_json"], ReviewRoundPlan)
    identity = plan.round
    key = identity.binding.rejection.fence.key
    if (row["plan_json"] != encode_workflow(plan) or row["round_id"] != identity.round_id
            or not isinstance(key.target, PullRequestRef) or row["repository"] != key.repository
            or row["target_kind"] != "pr" or row["target_number"] != key.target.number
            or row["execution_epoch"] != identity.binding.execution_epoch
            or row["execution_episode"] != identity.binding.execution_episode
            or row["generation"] != identity.binding.rejection.fence.generation
            or row["sequence"] != identity.sequence):
        raise ValueError("review round plan disagrees with its SQL identity")
    return plan


def verify_complete_legacy_submission_settlement_storage(
    db: sqlite3.Connection, observed_commands: CompleteLegacyReviewSubmissions,
    observed_receipts: CompleteLegacyReviewSubmissionReceipts,
    observed_round_storage: CompleteLegacyRoundOutcomeStorage,
) -> CompleteLegacySubmissionSettlementStorage | BlockedLegacySubmissionSettlementStorage:
    """Account for every command, registration, index and marker in one SQL snapshot."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("submission settlement storage requires an explicit SQL transaction")
    if (not isinstance(observed_commands, CompleteLegacyReviewSubmissions)
            or not isinstance(observed_receipts, CompleteLegacyReviewSubmissionReceipts)
            or not isinstance(observed_round_storage, CompleteLegacyRoundOutcomeStorage)):
        raise ValueError("submission settlement storage requires complete typed observations")
    repository = observed_commands.repository
    if repository != observed_receipts.repository or repository != observed_round_storage.repository:
        return BlockedLegacySubmissionSettlementStorage(repository, "submission and round repositories disagree")
    commands = revalidate_complete_legacy_review_submissions(db, observed_commands)
    if isinstance(commands, BlockedLegacyReviewSubmissions):
        return BlockedLegacySubmissionSettlementStorage(repository, commands.reason, commands.source_rowid)
    receipts = revalidate_complete_legacy_review_submission_receipts(db, observed_receipts)
    if isinstance(receipts, BlockedLegacyReviewSubmissionReceipts):
        return BlockedLegacySubmissionSettlementStorage(repository, receipts.reason, receipts.source_rowid)
    round_storage = verify_complete_legacy_round_outcome_storage(
        db, observed_round_storage.rounds, observed_round_storage.receipts,
    )
    if isinstance(round_storage, BlockedLegacyRoundOutcomeStorage):
        return BlockedLegacySubmissionSettlementStorage(repository, round_storage.reason, round_storage.source_rowid)
    command_by_id = {dict(source.command_fields)["command_id"]: source for source in commands.submissions}
    receipt_by_key = {(dict(source.fields)["namespace"], dict(source.fields)["key"]): source
                      for source in receipts.receipts}
    plan_by_id: dict[str, tuple[LegacyReviewRoundSource, ReviewRoundPlan]] = {}
    round_plans: list[tuple[LegacyReviewRoundSource, ReviewRoundPlan]] = []
    sealed_by_id = {entry.outcome.round.round_id: entry for entry in round_storage.sealed}
    for source in round_storage.rounds.rounds:
        try:
            plan = _stored_review_round_plan(source)
            sealed = sealed_by_id.get(plan.round.round_id)
            if sealed is not None and sealed.facts.plan != plan:
                raise ValueError("sealed review round plan disagrees with its closure")
        except (TypeError, ValueError, UnicodeError, WorkflowAuthorityError) as exc:
            return BlockedLegacySubmissionSettlementStorage(repository, _printable(str(exc)), source.source_rowid)
        plan_by_id[plan.round.round_id] = (source, plan)
        round_plans.append((source, plan))
    registered: list[VerifiedLegacySubmissionRegistrationStorage] = []
    registered_ids: set[str] = set()
    recorded_markers: list[LegacyReviewSubmissionReceiptSource] = []
    for source in receipts.receipts:
        row = dict(source.fields)
        namespace = row["namespace"]
        if namespace == "review-recorded-submission":
            if row["key"] not in command_by_id:
                return BlockedLegacySubmissionSettlementStorage(
                    repository, "recorded review marker has no submission command", source.source_rowid,
                )
            recorded_markers.append(source)
            continue
        if namespace != "review-round-submission":
            continue
        try:
            registration = decode_workflow(row["payload_json"], ReviewSubmissionRegistration)
            command_id = registration.submission.command_id
            command_source = command_by_id.get(command_id)
            planned = plan_by_id.get(registration.round.round_id)
            if (row["payload_json"] != encode_workflow(registration)
                    or row["key"] != command_id or command_source is None or planned is None):
                raise ValueError("review registration lacks its exact command or round")
            round_source, plan = planned
            command = dict(command_source.command_fields)
            if (command["repository"] != repository or command["target"] !=
                    f"pr:{registration.round.binding.rejection.fence.key.target.number}"
                    or command["expected_revision"] != plan.head_sha
                    or command["workflow_binding_json"] != encode_workflow(registration.round.binding.rejection)
                    or command["payload_digest"] != _encoded_payload_digest(
                        command["payload_json"], command["workflow_binding_json"],
                    )):
                raise ValueError("review registration disagrees with its bound command")
            index_key = review_submission_receipt_key(registration.round.round_id, command_id)
            index_source = receipt_by_key.get(("review-round-submission-index", index_key))
            if index_source is None:
                raise ValueError("review registration lacks its exact round index")
            index = dict(index_source.fields)
            if (index["payload_json"] != row["payload_json"] or index["claimed_at"] != row["claimed_at"]
                    or row["reusable_after"] is not None or index["reusable_after"] is not None):
                raise ValueError("review registration and round index disagree")
            _parse_stamp(row["claimed_at"])
            registered.append(VerifiedLegacySubmissionRegistrationStorage(
                command_source, source, index_source, round_source, plan, registration,
            ))
            registered_ids.add(command_id)
        except (KeyError, TypeError, ValueError, AttributeError, UnicodeError,
                WorkflowAuthorityError) as exc:
            return BlockedLegacySubmissionSettlementStorage(repository, _printable(str(exc)), source.source_rowid)
    unregistered = tuple(source for source in commands.submissions
                         if dict(source.command_fields)["command_id"] not in registered_ids)
    try:
        return CompleteLegacySubmissionSettlementStorage(
            commands, receipts, round_storage, tuple(round_plans), tuple(registered),
            unregistered, tuple(recorded_markers),
        )
    except ValueError as exc:
        return BlockedLegacySubmissionSettlementStorage(repository, _printable(str(exc)))


@dataclass(frozen=True)
class VerifiedLegacyStaleProductivityReceiptPath:
    """A successful command has the writer's stale receipt shape and no baseline marker.

    Historical generation ordering at settlement is not established by this receipt.
    """

    complete_storage: CompleteLegacySubmissionSettlementStorage
    registered: VerifiedLegacySubmissionRegistrationStorage
    sealed: VerifiedLegacyRoundOutcomeStorage
    stale_receipt: LegacyReviewOutcomeReceiptSource

    def __post_init__(self) -> None:
        if (not isinstance(self.complete_storage, CompleteLegacySubmissionSettlementStorage)
                or not isinstance(self.registered, VerifiedLegacySubmissionRegistrationStorage)
                or not isinstance(self.sealed, VerifiedLegacyRoundOutcomeStorage)
                or not isinstance(self.stale_receipt, LegacyReviewOutcomeReceiptSource)):
            raise ValueError("stale settlement requires typed complete storage")
        command = dict(self.registered.command_source.command_fields)
        command_id = command["command_id"]
        round_id = self.registered.plan.round.round_id
        effect_fields = self.registered.command_source.receipt_fields
        if effect_fields is None:
            raise ValueError("stale settlement lacks a confirmed effect")
        effect = dict(effect_fields)
        try:
            receipt = EffectReceipt(
                CommandId(command_id), effect["executor_identity"],
                _parse_stamp(effect["started_at"]), _parse_stamp(effect["completed_at"]),
                EffectStatus(effect["status"]), effect["remote_object_id"], effect["remote_revision"], effect["error"],
            )
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise ValueError("stale settlement has malformed effect evidence") from exc
        confirmed = tuple(item for item in self.sealed.facts.submissions
                          if isinstance(item.result, ReviewSubmissionConfirmed))
        disposition = self.sealed.outcome.disposition
        stale = dict(self.stale_receipt.fields)
        auxiliary = {"review-round-superseded-productivity", "review-round-ceiling-productivity"}
        if (self.registered not in self.complete_storage.registered
                or self.sealed not in self.complete_storage.round_storage.sealed
                or self.stale_receipt not in self.complete_storage.round_storage.receipts.receipts
                or any(dict(marker.fields)["key"] == command_id
                       for marker in self.complete_storage.recorded_markers)
                or any(dict(source.fields)["namespace"] in auxiliary
                       and dict(source.fields)["key"] == round_id
                       for source in self.complete_storage.round_storage.receipts.receipts)
                or command["state"] != "succeeded" or command["attempts"] < 1
                or command["attempt_owner"] != receipt.executor_identity
                or receipt.executor_identity != command["executor_identity"]
                or receipt.status is not EffectStatus.SUCCEEDED
                or not isinstance(receipt.remote_object_id, str) or not receipt.remote_object_id
                or sum(1 for source in self.complete_storage.commands.submissions
                       if source.receipt_fields is not None
                       and dict(source.receipt_fields)["remote_object_id"] == receipt.remote_object_id) != 1
                or effect["receipt_digest"] != _receipt_digest(receipt)
                or len(confirmed) != 1 or confirmed[0].plan.command_id != command_id
                or not isinstance(disposition, (PaidReviewProgress, CopiedReviewProgress))
                or confirmed[0].plan.kind is not (
                    ReviewSubmissionKind.PAID if isinstance(disposition, PaidReviewProgress)
                    else ReviewSubmissionKind.COPIED
                )
                or self.sealed.outcome.round != self.registered.plan.round
                or confirmed[0].result.receipt_id != disposition.submission_receipt
                or confirmed[0].result.receipt_id != _effect_receipt_id(
                    command_id, effect["receipt_digest"],
                )
                or stale["namespace"] != "review-round-stale-productivity"
                or stale["scope"] != self.complete_storage.repository
                or stale["key"] != round_id
                or stale["reusable_after"] is not None
                or stale["claimed_at"] != dict(self.sealed.receipt_source.fields)["claimed_at"]
                or stale["payload_json"] != encode_workflow((self.sealed.facts, self.sealed.outcome))):
            raise ValueError("stale settlement has contradictory command or round evidence")
        _parse_stamp(stale["claimed_at"])

    @property
    def source_digest(self) -> str:
        parts = (self.complete_storage.source_digest, self.registered.source_digest,
                 self.sealed.source_digest, self.stale_receipt.source_digest)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class UnverifiedLegacyStaleProductivityReceiptPath:
    command_id: str
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "unverified stale submission command")
        if not self.reason:
            raise ValueError("unverified stale submission requires a reason")


def verify_legacy_stale_productivity_receipt_path(
    db: sqlite3.Connection, observed_storage: CompleteLegacySubmissionSettlementStorage, command_id: str,
) -> VerifiedLegacyStaleProductivityReceiptPath | UnverifiedLegacyStaleProductivityReceiptPath:
    """Recheck complete SQL sets before classifying one recorded stale receipt path."""
    validate_workflow_identifier(command_id, "stale submission command")
    if not isinstance(observed_storage, CompleteLegacySubmissionSettlementStorage):
        raise ValueError("stale settlement requires complete typed storage")
    current = verify_complete_legacy_submission_settlement_storage(
        db, observed_storage.commands, observed_storage.receipts, observed_storage.round_storage,
    )
    if isinstance(current, BlockedLegacySubmissionSettlementStorage):
        return UnverifiedLegacyStaleProductivityReceiptPath(command_id, current.reason)
    registered = next((item for item in current.registered
                       if dict(item.command_source.command_fields)["command_id"] == command_id), None)
    if registered is None:
        return UnverifiedLegacyStaleProductivityReceiptPath(command_id, "submission lacks registered round storage")
    round_id = registered.plan.round.round_id
    sealed = next((item for item in current.round_storage.sealed
                   if item.outcome.round.round_id == round_id), None)
    stale = next((item for item in current.round_storage.receipts.receipts
                  if dict(item.fields)["namespace"] == "review-round-stale-productivity"
                  and dict(item.fields)["key"] == round_id), None)
    if sealed is None or stale is None:
        return UnverifiedLegacyStaleProductivityReceiptPath(command_id, "sealed stale settlement receipt is absent")
    try:
        return VerifiedLegacyStaleProductivityReceiptPath(current, registered, sealed, stale)
    except ValueError as exc:
        return UnverifiedLegacyStaleProductivityReceiptPath(command_id, _printable(str(exc)))


def read_complete_legacy_review_generation_receipts(
    db: sqlite3.Connection, repository: str,
) -> CompleteLegacyReviewGenerationReceipts | BlockedLegacyReviewGenerationReceipts:
    """Read every generation receipt under one bounded SQL snapshot."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("generation receipt scan requires an explicit SQL transaction")
    if canonical_workflow_repository(repository) != repository:
        raise ValueError("generation receipt scan requires a canonical repository")
    if db.execute(
        "SELECT rowid FROM dedupe_keys NOT INDEXED ORDER BY rowid LIMIT 1 OFFSET ?",
        (MAX_LEGACY_GENERATION_RECEIPT_SCAN_ROWS,),
    ).fetchone() is not None:
        return BlockedLegacyReviewGenerationReceipts(repository, "generation receipt table exceeds its scan bound")
    repository_bytes = repository.encode()
    namespace_bytes = b"review-execution-generation"
    sources: list[LegacyReviewGenerationReceiptSource] = []
    source_bytes = 0
    cursor: int | None = None
    while True:
        predicate = "" if cursor is None else "WHERE rowid>?"
        params = () if cursor is None else (cursor,)
        rows = db.execute(
            "SELECT rowid AS source_rowid,"
            "CASE WHEN length(CAST(namespace AS BLOB))<=64 THEN CAST(namespace AS BLOB) END AS namespace_bytes,"
            "CASE WHEN length(CAST(scope AS BLOB))<=? THEN CAST(scope AS BLOB) END AS scope_bytes "
            f"FROM dedupe_keys NOT INDEXED {predicate} ORDER BY rowid LIMIT 256",
            (max(len(repository_bytes) + 64, 64), *params),
        ).fetchall()
        if not rows:
            break
        cursor = rows[-1]["source_rowid"]
        matching_ids: list[int] = []
        for row in rows:
            if row["namespace_bytes"] != namespace_bytes:
                continue
            scope = row["scope_bytes"]
            if scope is None:
                return BlockedLegacyReviewGenerationReceipts(
                    repository, "generation receipt scope exceeds its attribution bound", row["source_rowid"],
                )
            if scope == repository_bytes:
                matching_ids.append(row["source_rowid"])
                continue
            try:
                alias = canonical_workflow_repository(scope.decode("utf-8"))
            except (UnicodeError, ValueError):
                continue
            if alias == repository:
                return BlockedLegacyReviewGenerationReceipts(
                    repository, "generation receipt uses a noncanonical repository alias", row["source_rowid"],
                )
        if not matching_ids:
            continue
        marks = ",".join("?" for _ in matching_ids)
        other = ("namespace", "scope", "key", "claimed_at", "reusable_after")
        sizes = [f"ifnull(length(CAST({column} AS BLOB)),0)" for column in other]
        payload_size = "ifnull(length(CAST(payload_json AS BLOB)),0)"
        metadata = db.execute(
            f"SELECT rowid AS source_rowid,max({','.join(sizes)}) AS largest_other,"
            f"{payload_size} AS payload_bytes,{'+'.join((*sizes, payload_size))} AS total_bytes "
            f"FROM dedupe_keys WHERE rowid IN ({marks}) ORDER BY rowid",
            matching_ids,
        ).fetchall()
        prospective_count = len(sources)
        prospective_bytes = source_bytes
        for row in metadata:
            rowid = row["source_rowid"]
            if (row["largest_other"] > MAX_LEGACY_SQL_TEXT_BYTES
                    or row["payload_bytes"] > MAX_LEGACY_GENERATION_RECEIPT_JSON_BYTES
                    or prospective_count >= MAX_LEGACY_GENERATION_RECEIPT_ROWS
                    or prospective_bytes + row["total_bytes"] > MAX_LEGACY_GENERATION_RECEIPT_BYTES):
                return BlockedLegacyReviewGenerationReceipts(
                    repository, "generation receipt exceeds its source bound", rowid,
                )
            prospective_count += 1
            prospective_bytes += row["total_bytes"]
        columns = ",".join(
            f"typeof({column}) AS type_{column},CAST({column} AS BLOB) AS raw_{column}"
            for column in LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS
        )
        values = {row["source_rowid"]: row for row in db.execute(
            f"SELECT rowid AS source_rowid,{columns} FROM dedupe_keys "
            f"WHERE rowid IN ({marks}) ORDER BY rowid", matching_ids,
        )}
        for row in metadata:
            rowid = row["source_rowid"]
            source = values.get(rowid)
            if source is None:
                return BlockedLegacyReviewGenerationReceipts(
                    repository, "generation receipt changed during its read", rowid,
                )
            try:
                fields = tuple((column, _sql_text(source[f"type_{column}"], source[f"raw_{column}"]))
                               for column in LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS)
                observed = LegacyReviewGenerationReceiptSource(rowid, fields)
                if dict(observed.fields)["scope"] != repository:
                    raise ValueError("generation receipt scope changed")
            except (TypeError, ValueError, UnicodeError) as exc:
                return BlockedLegacyReviewGenerationReceipts(repository, _printable(str(exc)), rowid)
            source_bytes += observed.source_bytes
            sources.append(observed)
    try:
        return CompleteLegacyReviewGenerationReceipts(repository, tuple(sources))
    except ValueError as exc:
        return BlockedLegacyReviewGenerationReceipts(repository, _printable(str(exc)))


def revalidate_complete_legacy_review_generation_receipts(
    db: sqlite3.Connection, observed: CompleteLegacyReviewGenerationReceipts,
) -> CompleteLegacyReviewGenerationReceipts | BlockedLegacyReviewGenerationReceipts:
    """Refuse a changed or partial caller-held generation receipt set."""
    if not isinstance(observed, CompleteLegacyReviewGenerationReceipts):
        raise ValueError("generation receipt revalidation requires a complete typed observation")
    current = read_complete_legacy_review_generation_receipts(db, observed.repository)
    if isinstance(current, BlockedLegacyReviewGenerationReceipts):
        return current
    if current.source_digest != observed.source_digest:
        return BlockedLegacyReviewGenerationReceipts(
            observed.repository, "generation receipt sources changed after inventory",
        )
    return current


@dataclass(frozen=True)
class MatchedLegacyReviewGenerationBoundarySources:
    """Observed content match; SQL provenance must be rechecked before authority use."""

    complete_receipts: CompleteLegacyReviewGenerationReceipts
    receipt_source: LegacyReviewGenerationReceiptSource
    boundary: ReviewExecutionGenerationBoundary
    accepted_rowid: int
    accepted_fields: tuple[tuple[str, str | None], ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.complete_receipts, CompleteLegacyReviewGenerationReceipts)
                or not isinstance(self.receipt_source, LegacyReviewGenerationReceiptSource)
                or not isinstance(self.boundary, ReviewExecutionGenerationBoundary)
                or self.receipt_source not in self.complete_receipts.receipts
                or type(self.accepted_rowid) is not int or self.accepted_rowid <= 0
                or not isinstance(self.accepted_fields, tuple)
                or tuple(name for name, _ in self.accepted_fields) != _DEDUPE_SOURCE_COLUMNS
                or any(value is not None and (not isinstance(value, str)
                       or len(value.encode("utf-8", "surrogatepass")) > MAX_LEGACY_RECORDING_BYTES)
                       for _, value in self.accepted_fields)):
            raise ValueError("generation boundary requires complete typed source evidence")
        source = dict(self.receipt_source.fields)
        accepted = dict(self.accepted_fields)
        if (self.receipt_source.source_rowid <= self.accepted_rowid
                or source["scope"] != self.complete_receipts.repository
                or source["key"] != self.boundary.receipt_id
                or source["reusable_after"] is not None
                or accepted["scope"] != self.complete_receipts.repository
                or accepted["reusable_after"] is not None
                or accepted["namespace"] not in {"workflow-request", "workflow-transition"}
                or accepted["payload_json"] is None
                or source["payload_json"] != encode_workflow((
                    self.boundary, accepted["namespace"], accepted["key"], accepted["payload_json"],
                ))):
            raise ValueError("generation boundary and accepted receipt disagree")
        _parse_stamp(source["claimed_at"])
        _parse_stamp(accepted["claimed_at"])
        key = self.boundary.from_revision.fence.key
        if not isinstance(key.target, PullRequestRef):
            raise ValueError("generation boundary requires a review PR")
        identity = (
            f"{key.repository}:{key.target.number}:"
            f"{self.boundary.before.binding.execution_epoch}:"
            f"{self.boundary.before.binding.execution_episode}:"
            f"{self.boundary.to_revision.fence.generation}"
        )
        derived_id = "review-generation:" + hashlib.sha256(identity.encode()).hexdigest()
        if (self.boundary.receipt_id != derived_id
                or self.boundary.from_revision.fence.key != self.boundary.to_revision.fence.key
                or key.repository != self.complete_receipts.repository):
            raise ValueError("generation boundary has a changed derived identity")
        try:
            raw = accepted["payload_json"]
            payload = json.loads(raw, object_pairs_hook=_unique_receipt_object, parse_constant=_reject_nonfinite)
            if (not isinstance(payload, dict)
                    or set(payload) != {"mutation", "expected", "snapshot", "audit", "commands"}
                    or json.dumps(payload, sort_keys=True, separators=(",", ":")) != raw
                    or not isinstance(payload["commands"], list)):
                raise ValueError("accepted generation transition has malformed fields")
            expected = decode_workflow(payload["expected"], WorkflowRevision)
            snapshot = decode_workflow(payload["snapshot"], ReviewWorkflowSnapshot)
            if (payload["mutation"] != self.boundary.mutation.value
                    or expected != self.boundary.from_revision
                    or snapshot.revision != self.boundary.to_revision
                    or not isinstance(snapshot.execution, ActivatedReviewExecution)
                    or not snapshot.execution.boundaries
                    or snapshot.execution.boundaries[-1] != self.boundary
                    or snapshot.execution.binding != self.boundary.after.binding
                    or snapshot.execution.state != self.boundary.after
                    or snapshot.binding.fence != self.boundary.to_revision.fence):
                raise ValueError("accepted generation transition disagrees with its boundary")
            commands = payload["commands"]
            if len(commands) > MAX_REARM_COMMANDS:
                raise ValueError("accepted generation transition has too many commands")
            decoded_commands: list[LifecycleCommand] = []
            for item in commands:
                if (not isinstance(item, dict) or set(item) != {
                    "id", "idempotency", "kind", "target", "revision", "payload",
                    "config", "repository", "owner", "workflow",
                } or not isinstance(item["payload"], dict)
                        or any(not isinstance(item[name], str) for name in (
                            "id", "idempotency", "kind", "target", "config",
                            "repository", "owner", "workflow",
                        ))
                        or (item["revision"] is not None and not isinstance(item["revision"], str))):
                    raise ValueError("accepted generation transition has malformed commands")
                command = LifecycleCommand(
                    item["id"], item["idempotency"], CommandKind(item["kind"]),
                    item["target"], item["revision"], item["payload"], item["config"],
                    item["repository"], item["owner"],
                    decode_workflow(item["workflow"], WorkflowBinding),
                )
                if (command.repository != self.complete_receipts.repository
                        or command.workflow != snapshot.binding
                        or command.repository != item["repository"]):
                    raise ValueError("accepted generation command has a changed workflow binding")
                decoded_commands.append(command)
            if len({command.command_id for command in decoded_commands}) != len(decoded_commands):
                raise ValueError("accepted generation transition repeats a command")
            if self.boundary.mutation in (WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM):
                audit = decode_workflow(payload["audit"], AuthorizedOperatorCommand)
                audit_kind = (RequestRearm if self.boundary.mutation is WorkflowMutation.REQUEST_REARM
                              else ContinueGrantedRearm)
                if (accepted["namespace"] != "workflow-request"
                        or not isinstance(audit.command, audit_kind)
                        or accepted["key"] != audit.command.request_id
                        or audit.command.expected != self.boundary.from_revision):
                    raise ValueError("generation request identity disagrees with accepted operator command")
            else:
                audit = decode_workflow(payload["audit"], ObservedTransition)
                if (not audit.evidence_ids or accepted["namespace"] != "workflow-transition"
                        or accepted["key"] != hashlib.sha256(
                            encode_workflow(self.boundary.from_revision).encode(),
                        ).hexdigest()):
                    raise ValueError("generation release identity disagrees with accepted transition")
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("generation boundary lacks an exact accepted transition") from exc

    @property
    def source_digest(self) -> str:
        parts = (self.complete_receipts.source_digest, self.receipt_source.source_digest,
                 self.accepted_rowid, self.accepted_fields)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class UnmatchedLegacyReviewGenerationBoundarySources:
    receipt_id: str
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.receipt_id, "unverified generation receipt")
        if not self.reason:
            raise ValueError("unverified generation receipt requires a reason")


def match_legacy_review_generation_boundary_sources(
    db: sqlite3.Connection, observed: CompleteLegacyReviewGenerationReceipts, receipt_id: str,
) -> MatchedLegacyReviewGenerationBoundarySources | UnmatchedLegacyReviewGenerationBoundarySources:
    """Observe a source match in SQL; callers must recheck at authority use."""
    validate_workflow_identifier(receipt_id, "generation receipt")
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("generation boundary verification requires an explicit SQL transaction")
    if not isinstance(observed, CompleteLegacyReviewGenerationReceipts):
        raise ValueError("generation boundary verification requires complete typed sources")
    current = revalidate_complete_legacy_review_generation_receipts(db, observed)
    if isinstance(current, BlockedLegacyReviewGenerationReceipts):
        return UnmatchedLegacyReviewGenerationBoundarySources(receipt_id, current.reason)
    source = next((entry for entry in current.receipts if dict(entry.fields)["key"] == receipt_id), None)
    if source is None:
        return UnmatchedLegacyReviewGenerationBoundarySources(receipt_id, "generation receipt is absent")
    try:
        values = dict(source.fields)
        captured = decode_workflow(values["payload_json"], tuple)
        if (len(captured) != 4 or not isinstance(captured[0], ReviewExecutionGenerationBoundary)
                or not isinstance(captured[1], str) or not isinstance(captured[2], str)
                or not isinstance(captured[3], str)):
            raise ValueError("generation receipt has malformed accepted transition fields")
        boundary, namespace, key, accepted_payload = captured
        rows = db.execute(
            "SELECT rowid,CASE WHEN length(CAST(scope AS BLOB))<=? "
            "THEN CAST(scope AS BLOB) END AS scope_bytes FROM dedupe_keys NOT INDEXED "
            "WHERE namespace IN (?,?) AND key IN (?,?) ORDER BY rowid LIMIT ?",
            (max(len(current.repository.encode()) + 64, 64), namespace, namespace.encode(),
             key, key.encode(), MAX_LEGACY_GENERATION_RECEIPT_ROWS + 1),
        ).fetchall()
        if len(rows) > MAX_LEGACY_GENERATION_RECEIPT_ROWS:
            raise ValueError("accepted generation transition exceeds its alias scan bound")
        accepted_rows: list[int] = []
        for row in rows:
            scope = row["scope_bytes"]
            if scope is None:
                raise ValueError("accepted generation transition scope exceeds its attribution bound")
            if scope == current.repository.encode():
                accepted_rows.append(row["rowid"])
                continue
            try:
                alias = canonical_workflow_repository(scope.decode("utf-8"))
            except (UnicodeError, ValueError):
                continue
            if alias == current.repository:
                raise ValueError("accepted generation transition uses a repository alias")
        if len(accepted_rows) != 1:
            raise ValueError("generation boundary lacks one exact accepted transition")
        accepted_rowid = accepted_rows[0]
        accepted_fields, _ = _read_recording_source(db, namespace, current.repository, key)
        if dict(accepted_fields)["payload_json"] != accepted_payload:
            raise ValueError("accepted generation transition changed")
        return MatchedLegacyReviewGenerationBoundarySources(
            current, source, boundary, accepted_rowid, accepted_fields,
        )
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, WorkflowAuthorityError) as exc:
        return UnmatchedLegacyReviewGenerationBoundarySources(receipt_id, _printable(str(exc)))
