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
from dataclasses import replace

from ..domain.resolve_activation import LegacyAbsent, LegacyFile, UnmappedLegacyEntry
from ..domain.review_execution_activation import (
    MAX_LEGACY_SQL_TEXT_BYTES, MAX_SQL_ROWID, MIN_SQL_ROWID, LegacyExecutionCounterRecord,
    LegacyExecutionEntry, LegacyExecutionPRRecord,
    LegacyExecutionRunPage, LegacyExecutionRunRecord, LegacyReviewRunState, UnreadableLegacyExecutionRun,
    CompleteLegacyReviewRuns, BlockedLegacyReviewRuns,
    MAX_LEGACY_RUN_INVENTORY_ROWS, MAX_LEGACY_RUN_INVENTORY_BYTES,
    LEGACY_SUBMISSION_COMMAND_COLUMNS, LEGACY_SUBMISSION_RECEIPT_COLUMNS,
    LegacyReviewSubmissionPage, LegacyReviewSubmissionSource, UnreadableLegacyReviewSubmission,
    LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS, LEGACY_REVIEW_SUBMISSION_RECEIPT_NAMESPACES,
    LegacyReviewSubmissionReceiptPage, LegacyReviewSubmissionReceiptSource,
    UnreadableLegacyReviewSubmissionReceipt,
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
)
from ..domain.contracts import CommandId, EffectReceipt, EffectStatus
from ..domain.review_execution import ExecutionNotActivated, ReviewRoundOutcome
from ..domain.review_workflow import ReviewWorkflowSnapshot
from ..domain.review_workflow_policy import ReviewSubmissionReceipt
from ..domain.review_rounds import (
    MAX_REVIEW_STAGES, ReviewRoundPlan, ReviewStagePlan, ReviewSubmissionKind, ReviewSubmissionRegistration,
    ReviewRoundFacts,
)
from ..domain.review_preflight import ReviewPreflightSelection
from ..domain.workflow import ObservedTransition, WorkflowMutation
from ..domain.workflow_identity import WorkflowRevision, canonical_workflow_repository, validate_workflow_identifier
from .control_store import CorruptControlStore, _encoded_payload_digest, _parse_stamp, _receipt_digest, load_bound_review_submission
from .resolve_activation import LegacyRoots, _printable, _read_legacy_file, distinct_roots
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
MAX_SUBMISSION_RECEIPT_SOURCE_PAGE = 64
MAX_REVIEW_ROUND_SOURCE_PAGE = 16
MAX_OUTCOME_RECEIPT_SOURCE_PAGE = 16
MAX_OUTCOME_RECEIPT_PAGE_BYTES = 2**24
MAX_OUTCOME_RECEIPT_SCAN_ROWS = 1_000_000
MAX_OUTCOME_RECEIPT_SCAN_CHUNK = 256
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
    predicate = "WHERE c.repository=? AND c.kind='submit_review'" + (
        " AND c.rowid>?" if after_rowid is not None else ""
    )
    params = (repository,) if after_rowid is None else (repository, after_rowid)
    metadata = db.execute(
        f"SELECT c.rowid AS source_rowid,max({sizes}) AS largest_value "
        "FROM lifecycle_commands c LEFT JOIN effect_receipts e ON e.command_id=c.command_id "
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
            f"WHERE c.repository=? AND c.kind='submit_review' AND c.rowid IN ({placeholders})",
            (repository, *safe_ids),
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
    placeholders = ",".join("?" for _ in namespaces)
    predicate = f"WHERE scope=? AND namespace IN ({placeholders})" + (
        " AND rowid>?" if after_rowid is not None else ""
    )
    params = (repository, *namespaces) if after_rowid is None else (repository, *namespaces, after_rowid)
    metadata = db.execute(
        f"SELECT rowid AS source_rowid,max({sizes}) AS largest_value FROM dedupe_keys "
        f"{predicate} ORDER BY rowid LIMIT ?",
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
            f"WHERE scope=? AND namespace IN ({placeholders}) AND rowid IN ({rowids})",
            (repository, *namespaces, *safe_ids),
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
                or not isinstance(after.execution, ExecutionNotActivated)):
            raise ValueError("accepted transition is not an inactive review baseline")
        return VerifiedLegacyRecordedReview(source, lineage, bound, expected, recorded,
                                            after.episode.continuation, audit,
                                            recorded_digest, transition_digest)
    except (KeyError, TypeError, ValueError, UnicodeError, sqlite3.Error,
            WorkflowAuthorityError, CorruptControlStore) as exc:
        return UnverifiedLegacyRecordedReview(command_id, str(exc)[:512] or "recorded review source is unreadable")


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
        return UnverifiedReviewSubmissionRoundLink(command_id, str(exc)[:512] or "review round link is unreadable")
