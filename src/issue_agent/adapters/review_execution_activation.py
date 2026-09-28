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
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from ..domain.resolve_activation import LegacyAbsent, LegacyFile, UnmappedLegacyEntry
from ..domain.review_execution_activation import (
    MAX_LEGACY_SQL_TEXT_BYTES, MAX_SQL_ROWID, MIN_SQL_ROWID, LegacyExecutionCounterRecord,
    LegacyExecutionEntry, LegacyExecutionPRRecord,
    LegacyExecutionRunPage, LegacyExecutionRunRecord, LegacyReviewRunState, UnreadableLegacyExecutionRun,
    CompleteLegacyReviewRuns, BlockedLegacyReviewRuns,
    MAX_LEGACY_RUN_INVENTORY_ROWS, MAX_LEGACY_RUN_INVENTORY_BYTES,
    LEGACY_SUBMISSION_COMMAND_COLUMNS, LEGACY_SUBMISSION_RECEIPT_COLUMNS,
    LegacyReviewSubmissionPage, LegacyReviewSubmissionSource, UnreadableLegacyReviewSubmission,
    potentially_successful_review_submission,
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
    OpenLegacyOrderedRound, FinalizedLegacyUnproductiveRound, FinalizedLegacyProductiveRound,
    LegacyOrderedExecutionEpisode,
    BlockedLegacySubmissionSettlementStorage,
    LegacyReviewGenerationReceiptSource, CompleteLegacyReviewGenerationReceipts,
    BlockedLegacyReviewGenerationReceipts, MAX_LEGACY_GENERATION_RECEIPT_ROWS,
    MAX_LEGACY_GENERATION_RECEIPT_BYTES, MAX_LEGACY_GENERATION_RECEIPT_JSON_BYTES,
)
from ..domain.contracts import CommandId, CommandKind, EffectReceipt, EffectStatus, LifecycleCommand
from ..domain.review_execution import (
    ExecutionNotActivated, ActivatedReviewExecution, ReviewRoundIdentity, ReviewRoundOutcome,
    PaidReviewProgress, CopiedReviewProgress, ReviewExecutionBinding, ReviewExecutionGenerationBoundary,
    MAX_EXECUTION_SUFFIX, replay_review_execution_generations,
    review_generation_chain_digest, same_execution_episode,
)
from ..domain.review_workflow import (
    ReviewWorkflowSnapshot, RecordedPullRequest, ReviewedBaseline, PullRequestState,
)
from ..domain.review_workflow_policy import (
    ReviewSubmissionReceipt, CeilingEscalation, CeilingProductivityDecision,
    OBSERVATION_MAX_AGE_SECONDS, _escalation_projection, open_review_ceiling,
)
from ..domain.labels import Label
from ..domain.review_rounds import (
    MAX_REVIEW_STAGES, ReviewRoundPlan, ReviewStagePlan, ReviewSubmissionKind, ReviewSubmissionRegistration,
    ReviewRoundFacts,
    DeferredReviewPreparation, ReviewSubmissionConfirmed, finalize_review_round,
    StaleReviewProductivitySettlement,
)
from ..domain.review_preflight import ReviewPreflightSelection
from ..domain.workflow import (
    ObservedTransition, WorkflowChanged, WorkflowMutation, AuthorizedOperatorCommand,
    RequestRearm, ContinueGrantedRearm,
)
from ..domain.workflow_identity import (
    PullRequestRef, WorkflowBinding, WorkflowRevision, WorkflowKey, FamilyAuthority, MAX_REARM_COMMANDS,
    canonical_workflow_repository, validate_workflow_identifier, validate_workflow_text,
)
from .control_store import (
    CorruptControlStore, _command_from_row, _encoded_payload_digest, _parse_stamp,
    _receipt_digest, load_bound_review_submission,
)
from .resolve_activation import LegacyRoots, _printable, _read_legacy_file, distinct_roots
from .review_round_sources import _effect_receipt_id
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_commit import _unique_receipt_object
from .workflow_rows import (
    WorkflowAuthorityError, load_review_workflow_row, review_stage_receipt_key, review_stage_receipt_prefix,
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
        if stale is not None:
            try:
                raw_stale = dict(stale.fields)["payload_json"]
                captured_stale = decode_workflow(raw_stale, tuple | StaleReviewProductivitySettlement)
                valid_stale = (
                    captured_stale == (proof.facts, proof.outcome)
                    if isinstance(captured_stale, tuple)
                    else captured_stale.facts == proof.facts and captured_stale.outcome == proof.outcome
                ) and raw_stale == encode_workflow(captured_stale)
            except (TypeError, ValueError, UnicodeError, RecursionError):
                valid_stale = False
            if not valid_stale:
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
    aliases = db.execute(
        "SELECT rowid,typeof(namespace) AS namespace_type,typeof(scope) AS scope_type,"
        "typeof(key) AS key_type FROM dedupe_keys "
        "WHERE namespace IN (?,CAST(? AS BLOB)) AND scope IN (?,CAST(? AS BLOB)) "
        "AND key IN (?,CAST(? AS BLOB)) LIMIT 2",
        (namespace, namespace, repository, repository, key, key),
    ).fetchall()
    if len(aliases) != 1 or any(aliases[0][f"{name}_type"] != "text"
                                for name in ("namespace", "scope", "key")):
        raise ValueError(f"{namespace} source has absent or aliased identity")
    sizes = ",".join(f"ifnull(length(CAST({column} AS BLOB)),0)" for column in _DEDUPE_SOURCE_COLUMNS)
    metadata = db.execute(
        f"SELECT rowid AS source_rowid,max({sizes}) AS largest_value FROM dedupe_keys "
        "WHERE namespace=? AND scope=? AND key=?", (namespace, repository, key),
    ).fetchone()
    if metadata is None or metadata["source_rowid"] != aliases[0]["rowid"]:
        raise ValueError(f"{namespace} source changed during its read")
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


def _bounded_identity_row(
    db: sqlite3.Connection, table: str, key_column: str, key: str,
    columns: tuple[str, ...], max_field_bytes: int,
) -> sqlite3.Row:
    sizes = ",".join(f"ifnull(length(CAST({column} AS BLOB)),0)" for column in columns)
    metadata = db.execute(
        f"SELECT rowid AS source_rowid,max({sizes}) AS largest_value FROM {table} WHERE {key_column}=?",
        (key,),
    ).fetchone()
    if metadata is None or metadata["source_rowid"] is None:
        raise ValueError(f"{table} source is absent")
    if metadata["largest_value"] > max_field_bytes:
        raise ValueError(f"{table} source exceeds its field bound")
    row = db.execute(
        f"SELECT rowid AS source_rowid,* FROM {table} WHERE rowid=? AND {key_column}=?",
        (metadata["source_rowid"], key),
    ).fetchone()
    if row is None:
        raise ValueError(f"{table} source changed during its read")
    return row


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
    return _read_legacy_recorded_review(db, source, lineage, None)


def _read_legacy_recorded_review(
    db: sqlite3.Connection, source: LegacyReviewSubmissionSource,
    lineage: LegacyReviewOwnerLineage, unique_remote_ids: frozenset[str] | None,
) -> VerifiedLegacyRecordedReview | UnverifiedLegacyRecordedReview:
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
        bound = _verified_successful_review_receipt(db, source, lineage, unique_remote_ids)
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


def _verified_successful_review_submission(
    db: sqlite3.Connection, source: LegacyReviewSubmissionSource,
    lineage: LegacyReviewOwnerLineage, unique_remote_ids: frozenset[str] | None,
) -> ReviewSubmissionReceipt | CeilingEscalation:
    command = dict(source.command_fields)
    effect = dict(source.receipt_fields) if source.receipt_fields is not None else None
    if command["repository"] != lineage.repository or effect is None:
        raise ValueError("submission lacks a same-repository effect receipt")
    _recheck_submission_source(db, source)
    if command["state"] != "succeeded" or effect["status"] != "succeeded":
        raise ValueError("submission lacks a successful effect")
    if (command["attempts"] < 1 or command["workflow_binding_json"] is None
            or command["payload_digest"] != _encoded_payload_digest(
                command["payload_json"], command["workflow_binding_json"],
            )):
        raise ValueError("submission command lacks its bound payload or paid attempt")
    receipt = EffectReceipt(
        CommandId(command["command_id"]), effect["executor_identity"],
        _parse_stamp(effect["started_at"]), _parse_stamp(effect["completed_at"]),
        EffectStatus(effect["status"]), effect["remote_object_id"], effect["remote_revision"], effect["error"],
    )
    if (command["attempt_owner"] != receipt.executor_identity
            or effect["receipt_digest"] != _receipt_digest(receipt)):
        raise ValueError("submission effect digest or attempt owner disagrees")
    if unique_remote_ids is None:
        duplicate_review = db.execute(
            "SELECT e.command_id FROM effect_receipts e JOIN lifecycle_commands c USING(command_id) "
            "WHERE e.remote_object_id=? AND c.kind='submit_review' AND c.repository=? LIMIT 2",
            (receipt.remote_object_id, lineage.repository),
        ).fetchall()
        if len(duplicate_review) != 1 or duplicate_review[0]["command_id"] != command["command_id"]:
            raise ValueError("submission review identity is shared by another command")
    elif receipt.remote_object_id not in unique_remote_ids:
        raise ValueError("submission review identity is shared by another command")
    bound = load_bound_review_submission(db, CommandId(command["command_id"]))
    if not isinstance(bound, (ReviewSubmissionReceipt, CeilingEscalation)):
        raise ValueError("submission is not a bound review")
    return bound


def _verified_successful_review_receipt(
    db: sqlite3.Connection, source: LegacyReviewSubmissionSource,
    lineage: LegacyReviewOwnerLineage, unique_remote_ids: frozenset[str] | None,
) -> ReviewSubmissionReceipt:
    bound = _verified_successful_review_submission(db, source, lineage, unique_remote_ids)
    if not isinstance(bound, ReviewSubmissionReceipt):
        raise ValueError("submission is not a bound verdict review")
    return bound


@dataclass(frozen=True)
class MatchedLegacyEscalatedReviewHoldPlan:
    """Accepted baseline and one durable hold obligation; delivery is separate."""

    source: LegacyReviewSubmissionSource
    lineage: LegacyReviewOwnerLineage
    submission: ReviewSubmissionReceipt
    recorded_revision: WorkflowRevision
    snapshot: ReviewWorkflowSnapshot
    audit: ObservedTransition
    observation: RecordedPullRequest
    projection: LifecycleCommand
    recorded_source_digest: str
    transition_source_digest: str
    observation_source_digest: str
    projection_source_digest: str

    def __post_init__(self) -> None:
        if (not isinstance(self.source, LegacyReviewSubmissionSource)
                or not isinstance(self.lineage, LegacyReviewOwnerLineage)
                or not isinstance(self.submission, ReviewSubmissionReceipt) or not self.submission.escalated
                or not isinstance(self.recorded_revision, WorkflowRevision)
                or not isinstance(self.snapshot, ReviewWorkflowSnapshot)
                or not isinstance(self.audit, ObservedTransition)
                or not isinstance(self.observation, RecordedPullRequest)
                or not isinstance(self.projection, LifecycleCommand)):
            raise ValueError("escalated hold plan requires typed accepted sources")
        for digest in (self.recorded_source_digest, self.transition_source_digest,
                       self.observation_source_digest, self.projection_source_digest):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("escalated hold plan requires source digests")
        command = dict(self.source.command_fields)
        effect = dict(self.source.receipt_fields) if self.source.receipt_fields is not None else {}
        baseline = self.snapshot.episode.continuation
        bound = self.submission.submission
        if (command["command_id"] != str(bound.command_id)
                or command["repository"] != self.lineage.repository
                or command["executor_identity"] not in self.lineage.owners
                or effect.get("executor_identity") != command["executor_identity"]
                or command["attempt_owner"] != effect.get("executor_identity")
                or effect.get("remote_object_id") != self.submission.receipt_id.removeprefix("github-review:")
                or self.snapshot.revision != self.recorded_revision
                or self.snapshot.binding != bound.binding
                or not isinstance(baseline, ReviewedBaseline)
                or baseline.source != bound or baseline.head_sha != self.submission.head_sha
                or baseline.patch_id != self.submission.patch_id
                or self.audit.evidence_ids[:2] != (command["command_id"], self.submission.receipt_id)
                or len(self.audit.evidence_ids) != 3
                or self.observation.observation.key != bound.binding.fence.key
                or self.observation.observation.head_sha != self.submission.head_sha
                or self.observation.observation.state is not PullRequestState.OPEN
                or self.projection.repository != self.lineage.repository
                or self.projection.executor_identity not in self.lineage.owners
                or self.projection.workflow != self.snapshot.binding):
            raise ValueError("escalated hold plan disagrees with its submission or workflow")

    @property
    def source_digest(self) -> str:
        parts = (self.source.source_digest, self.lineage.source_digest,
                 self.recorded_source_digest, self.transition_source_digest,
                 self.observation_source_digest, self.projection_source_digest)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class UnmatchedLegacyEscalatedReviewHoldPlan:
    command_id: str
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "unmatched escalation submission")
        validate_workflow_text(self.reason, "unmatched escalation hold reason")


def match_legacy_escalated_review_hold_plan(
    db: sqlite3.Connection, source: LegacyReviewSubmissionSource,
    lineage: LegacyReviewOwnerLineage,
) -> MatchedLegacyEscalatedReviewHoldPlan | UnmatchedLegacyEscalatedReviewHoldPlan:
    """Match a verdict escalation to its committed baseline and relabel plan."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("escalated hold match requires an explicit SQL transaction")
    if not isinstance(source, LegacyReviewSubmissionSource) or not isinstance(lineage, LegacyReviewOwnerLineage):
        raise ValueError("escalated hold match requires typed command and owner sources")
    current = read_legacy_review_owner_lineage(
        db, lineage.repository, lineage.current_owner,
        configured_prior_owners=lineage.configured_prior_owners,
    )
    if current != lineage:
        return UnmatchedLegacyEscalatedReviewHoldPlan(
            dict(source.command_fields)["command_id"], "review owner lineage changed after inventory",
        )
    return _match_legacy_escalated_review_hold_plan(db, source, lineage, None)


def _match_legacy_escalated_review_hold_plan(
    db: sqlite3.Connection, source: LegacyReviewSubmissionSource,
    lineage: LegacyReviewOwnerLineage, unique_remote_ids: frozenset[str] | None,
) -> MatchedLegacyEscalatedReviewHoldPlan | UnmatchedLegacyEscalatedReviewHoldPlan:
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("escalated hold match requires an explicit SQL transaction")
    if not isinstance(source, LegacyReviewSubmissionSource) or not isinstance(lineage, LegacyReviewOwnerLineage):
        raise ValueError("escalated hold match requires typed command and owner sources")
    command_id = dict(source.command_fields)["command_id"]
    try:
        bound = _verified_successful_review_receipt(db, source, lineage, unique_remote_ids)
        if not bound.escalated:
            raise ValueError("submission is not an escalated verdict")
        recorded_fields, recorded_digest = _read_recording_source(
            db, "review-recorded-submission", lineage.repository, command_id,
        )
        recorded = decode_workflow(dict(recorded_fields)["payload_json"], WorkflowRevision)
        if recorded.version == 0:
            raise ValueError("escalated review has no predecessor revision")
        expected = WorkflowRevision(recorded.fence, recorded.version - 1)
        transition_key = hashlib.sha256(encode_workflow(expected).encode()).hexdigest()
        transition_fields, transition_digest = _read_recording_source(
            db, "workflow-transition", lineage.repository, transition_key,
        )
        for fields in (recorded_fields, transition_fields):
            receipt = dict(fields)
            if receipt["reusable_after"] is not None:
                raise ValueError("escalated baseline source is reclaimable")
            _parse_stamp(receipt["claimed_at"])
        accepted = json.loads(dict(transition_fields)["payload_json"],
                              object_pairs_hook=_unique_receipt_object, parse_constant=_reject_nonfinite)
        if (not isinstance(accepted, dict)
                or set(accepted) != {"mutation", "expected", "snapshot", "audit", "commands"}
                or accepted["mutation"] != WorkflowMutation.RECORD_REVIEW.value
                or not isinstance(accepted["commands"], list)
                or len(accepted["commands"]) != 1
                or decode_workflow(accepted["expected"], WorkflowRevision) != expected):
            raise ValueError("accepted transition is not an escalated review with one hold plan")
        after = decode_workflow(accepted["snapshot"], ReviewWorkflowSnapshot)
        audit = decode_workflow(accepted["audit"], ObservedTransition)
        if (after.revision != recorded or after.binding != bound.submission.binding
                or len(audit.evidence_ids) != 3
                or audit.evidence_ids[:2] != (command_id, bound.receipt_id)
                or (not isinstance(after.execution, ExecutionNotActivated)
                    and (not isinstance(after.execution, ActivatedReviewExecution)
                         or after.execution.binding.rejection != bound.submission.binding))):
            raise ValueError("accepted escalation disagrees with its bound review")
        key = bound.submission.binding.fence.key
        evidence_id = audit.evidence_ids[2]
        evidence_columns = (
            "evidence_id", "repository", "target_kind", "target_number", "command_id", "kind", "payload_json",
        )
        evidence = _bounded_identity_row(
            db, "workflow_evidence", "evidence_id", evidence_id,
            evidence_columns, MAX_LEGACY_RECORDING_BYTES,
        )
        if any(type(evidence[name]) is not str for name in (
            "evidence_id", "repository", "target_kind", "kind", "payload_json",
        )) or not isinstance(key.target, PullRequestRef) or (
            evidence["repository"], evidence["target_kind"], evidence["target_number"],
            evidence["command_id"], evidence["kind"],
        ) != (key.repository, "pr", key.target.number, None, "observation"):
            raise ValueError("escalated hold lacks its exact pull request observation")
        observation = decode_workflow(evidence["payload_json"], RecordedPullRequest)
        if (evidence_id != "pr-observation:" + hashlib.sha256(encode_workflow(observation).encode()).hexdigest()
                or observation.observation.key != key
                or observation.observation.head_sha != bound.head_sha
                or observation.observation.state is not PullRequestState.OPEN):
            raise ValueError("escalated hold observation disagrees with its receipt")
        projected = accepted["commands"][0]
        if not isinstance(projected, dict) or not isinstance(projected.get("id"), str):
            raise ValueError("escalated hold plan lacks a command identity")
        projection_row = _bounded_identity_row(
            db, "lifecycle_commands", "command_id", projected["id"],
            LEGACY_SUBMISSION_COMMAND_COLUMNS, MAX_LEGACY_SQL_TEXT_BYTES,
        )
        projection = _command_from_row(projection_row)
        if projection_row["payload_digest"] != _encoded_payload_digest(
            projection_row["payload_json"], projection_row["workflow_binding_json"],
        ):
            raise ValueError("durable hold projection has a corrupt payload digest")
        projection_fields = {
            "id": projection.command_id, "idempotency": projection.idempotency_key,
            "kind": projection.kind.value, "target": projection.target,
            "revision": projection.expected_revision, "payload": dict(projection.payload),
            "config": projection.config_version, "repository": projection.repository,
            "owner": projection.executor_identity, "workflow": encode_workflow(projection.workflow),
        }
        if (projected != projection_fields
                or projection_row["logical_key"] != projection.idempotency_key
                or projection_row["occurrence"] != 0
                or projection.kind is not CommandKind.RELABEL
                or projection.executor_identity not in lineage.owners):
            raise ValueError("durable hold projection disagrees with accepted plan")
        trigger = Label(projection.payload["remove"])
        expected_projection = _escalation_projection(
            after, observation.observation, bound.submission.command_id,
            escalation=command_id, trigger_label=trigger,
            executor_identity=projection.executor_identity,
        )
        if projection != expected_projection:
            raise ValueError("accepted relabel is not the guarded escalation hold")
        observation_fields = tuple((name, evidence[name]) for name in evidence_columns)
        command_fields = tuple((name, projection_row[name]) for name in LEGACY_SUBMISSION_COMMAND_COLUMNS)
        observation_digest = hashlib.sha256(json.dumps(
            (evidence["source_rowid"], observation_fields), separators=(",", ":"),
        ).encode()).hexdigest()
        projection_digest = hashlib.sha256(json.dumps(
            (projection_row["source_rowid"], command_fields), separators=(",", ":"),
        ).encode()).hexdigest()
        return MatchedLegacyEscalatedReviewHoldPlan(
            source, lineage, bound, recorded, after, audit, observation, projection,
            recorded_digest, transition_digest, observation_digest, projection_digest,
        )
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, sqlite3.Error,
            WorkflowAuthorityError, CorruptControlStore) as exc:
        return UnmatchedLegacyEscalatedReviewHoldPlan(command_id, _printable(str(exc)))


@dataclass(frozen=True)
class VerifiedLegacyEscalatedProductiveRound:
    """A confirmed productive round with its accepted escalation hold plan."""

    complete_storage: CompleteLegacyRoundOutcomeStorage
    storage: VerifiedLegacyRoundOutcomeStorage
    link: VerifiedReviewSubmissionRoundLink
    hold_plan: MatchedLegacyEscalatedReviewHoldPlan
    effect_receipt_id: str

    def __post_init__(self) -> None:
        if (not isinstance(self.complete_storage, CompleteLegacyRoundOutcomeStorage)
                or not isinstance(self.storage, VerifiedLegacyRoundOutcomeStorage)
                or not isinstance(self.link, VerifiedReviewSubmissionRoundLink)
                or not isinstance(self.hold_plan, MatchedLegacyEscalatedReviewHoldPlan)):
            raise ValueError("escalated productive round requires typed source proofs")
        validate_workflow_identifier(self.effect_receipt_id, "escalated productive effect identity")
        round_id = self.storage.outcome.round.round_id
        conflicting = {"review-round-stale-productivity", "review-round-superseded-productivity",
                       "review-round-ceiling-productivity"}
        confirmed = tuple(item for item in self.storage.facts.submissions
                          if isinstance(item.result, ReviewSubmissionConfirmed))
        disposition = self.storage.outcome.disposition
        execution = self.hold_plan.snapshot.execution
        if (self.complete_storage._sealed_by_id.get(round_id) != self.storage
                or bool(self.complete_storage._receipt_names_by_round.get(round_id, frozenset()) & conflicting)
                or len(confirmed) != 1 or not isinstance(disposition, (PaidReviewProgress, CopiedReviewProgress))
                or self.storage.outcome.round != self.link.round
                or self.link.source != self.hold_plan.source or self.link.lineage != self.hold_plan.lineage
                or str(confirmed[0].plan.command_id) != dict(self.link.source.command_fields)["command_id"]
                or confirmed[0].result.receipt_id != disposition.submission_receipt
                or confirmed[0].result.receipt_id != self.effect_receipt_id
                or confirmed[0].plan.kind is not (
                    ReviewSubmissionKind.PAID if isinstance(disposition, PaidReviewProgress)
                    else ReviewSubmissionKind.COPIED
                )
                or self.hold_plan.recorded_revision.fence != self.link.round.binding.rejection.fence
                or not isinstance(execution, ActivatedReviewExecution)
                or execution.binding != self.link.round.binding):
            raise ValueError("escalated productive round has contradictory submission or hold evidence")

    @property
    def source_digest(self) -> str:
        parts = (self.complete_storage.source_digest, self.storage.source_digest,
                 self.link.source_digest, self.hold_plan.source_digest, self.effect_receipt_id)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


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
    _registered_by_id: Mapping[str, VerifiedLegacySubmissionRegistrationStorage] = field(
        init=False, repr=False, compare=False,
    )
    _recorded_marker_ids: frozenset[str] = field(init=False, repr=False, compare=False)
    _unique_remote_review_ids: frozenset[str] = field(init=False, repr=False, compare=False)

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
        remote_counts: dict[str, int] = {}
        for source in self.commands.submissions:
            effect = dict(source.receipt_fields) if source.receipt_fields is not None else {}
            remote_id = effect.get("remote_object_id")
            if isinstance(remote_id, str):
                remote_counts[remote_id] = remote_counts.get(remote_id, 0) + 1
        object.__setattr__(self, "_registered_by_id", MappingProxyType(registered_by_id))
        object.__setattr__(self, "_recorded_marker_ids", frozenset(marker_keys))
        object.__setattr__(self, "_unique_remote_review_ids", frozenset(
            remote_id for remote_id, count in remote_counts.items() if count == 1
        ))

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
        try:
            stale_payload = decode_workflow(stale["payload_json"], tuple | StaleReviewProductivitySettlement)
            stale_matches = (
                stale_payload == (self.sealed.facts, self.sealed.outcome)
                if isinstance(stale_payload, tuple)
                else stale_payload.facts == self.sealed.facts
                and stale_payload.outcome == self.sealed.outcome
            ) and stale["payload_json"] == encode_workflow(stale_payload)
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("stale settlement has malformed receipt evidence") from exc
        auxiliary = {"review-round-superseded-productivity", "review-round-ceiling-productivity"}
        if (self.complete_storage._registered_by_id.get(command_id) != self.registered
                or self.complete_storage.round_storage._sealed_by_id.get(round_id) != self.sealed
                or self.complete_storage.round_storage._receipt_by_key.get(
                    ("review-round-stale-productivity", round_id),
                ) != self.stale_receipt
                or command_id in self.complete_storage._recorded_marker_ids
                or bool(self.complete_storage.round_storage._receipt_names_by_round.get(
                    round_id, frozenset(),
                ) & auxiliary)
                or command["state"] != "succeeded" or command["attempts"] < 1
                or command["attempt_owner"] != receipt.executor_identity
                or receipt.executor_identity != command["executor_identity"]
                or receipt.status is not EffectStatus.SUCCEEDED
                or not isinstance(receipt.remote_object_id, str) or not receipt.remote_object_id
                or receipt.remote_object_id not in self.complete_storage._unique_remote_review_ids
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
                or not stale_matches):
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
    return _stale_productivity_path_from_storage(current, command_id)


def _stale_productivity_path_from_storage(
    current: CompleteLegacySubmissionSettlementStorage, command_id: str,
) -> VerifiedLegacyStaleProductivityReceiptPath | UnverifiedLegacyStaleProductivityReceiptPath:
    registered = current._registered_by_id.get(command_id)
    if registered is None:
        return UnverifiedLegacyStaleProductivityReceiptPath(command_id, "submission lacks registered round storage")
    round_id = registered.plan.round.round_id
    sealed = current.round_storage._sealed_by_id.get(round_id)
    stale = current.round_storage._receipt_by_key.get(("review-round-stale-productivity", round_id))
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
        receipts = self.complete_receipts.receipts if isinstance(
            self.complete_receipts, CompleteLegacyReviewGenerationReceipts
        ) else ()
        receipt_index = bisect_left(receipts, self.receipt_source.source_rowid, key=lambda item: item.source_rowid) \
            if isinstance(self.receipt_source, LegacyReviewGenerationReceiptSource) else len(receipts)
        if (not isinstance(self.complete_receipts, CompleteLegacyReviewGenerationReceipts)
                or not isinstance(self.receipt_source, LegacyReviewGenerationReceiptSource)
                or not isinstance(self.boundary, ReviewExecutionGenerationBoundary)
                or receipt_index == len(receipts) or receipts[receipt_index] != self.receipt_source
                or type(self.accepted_rowid) is not int
                or not MIN_SQL_ROWID <= self.accepted_rowid <= MAX_SQL_ROWID
                or not isinstance(self.accepted_fields, tuple)
                or tuple(name for name, _ in self.accepted_fields) != _DEDUPE_SOURCE_COLUMNS
                or any(value is not None and (not isinstance(value, str)
                       or len(value.encode("utf-8", "surrogatepass")) > MAX_LEGACY_RECORDING_BYTES)
                       for _, value in self.accepted_fields)):
            raise ValueError("generation boundary requires complete typed source evidence")
        source = dict(self.receipt_source.fields)
        accepted = dict(self.accepted_fields)
        if (source["scope"] != self.complete_receipts.repository
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


@dataclass(frozen=True)
class MatchedLegacyStaleProductivityGenerationSources:
    """Observed stale path and its complete accepted generation crossing.

    SQL provenance must be rechecked before this observation grants authority.
    """

    stale_path: VerifiedLegacyStaleProductivityReceiptPath
    settlement: StaleReviewProductivitySettlement
    complete_receipts: CompleteLegacyReviewGenerationReceipts
    boundaries: tuple[MatchedLegacyReviewGenerationBoundarySources, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.stale_path, VerifiedLegacyStaleProductivityReceiptPath)
                or not isinstance(self.settlement, StaleReviewProductivitySettlement)
                or not isinstance(self.complete_receipts, CompleteLegacyReviewGenerationReceipts)
                or not isinstance(self.boundaries, tuple) or not self.boundaries
                or len(self.boundaries) > MAX_EXECUTION_SUFFIX
                or any(not isinstance(item, MatchedLegacyReviewGenerationBoundarySources)
                       or item.complete_receipts != self.complete_receipts for item in self.boundaries)):
            raise ValueError("stale generation crossing requires complete typed sources")
        round_id = self.stale_path.registered.plan.round
        stale = dict(self.stale_path.stale_receipt.fields)
        if (self.stale_path.complete_storage.repository != self.complete_receipts.repository
                or self.settlement.facts != self.stale_path.sealed.facts
                or self.settlement.outcome != self.stale_path.sealed.outcome
                or stale["payload_json"] != encode_workflow(self.settlement)):
            raise ValueError("stale generation crossing disagrees with its round receipt")
        generation = round_id.binding.rejection.fence.generation
        previous_version = -1
        for item in self.boundaries:
            boundary = item.boundary
            if (not same_execution_episode(round_id.binding, boundary.before.binding)
                    or not same_execution_episode(round_id.binding, boundary.after.binding)
                    or boundary.from_revision.fence.generation != generation
                    or boundary.to_revision.fence.generation != generation + 1
                    or boundary.from_revision.version <= previous_version
                    or boundary.to_revision.version > self.settlement.settled_revision.version):
                raise ValueError("stale generation crossing has a missing or unordered boundary")
            previous_version = boundary.to_revision.version
            generation += 1
        if (generation != self.settlement.settled_revision.fence.generation
                or self.boundaries[-1].boundary.after.binding != self.settlement.settled_binding
                or review_generation_chain_digest(tuple(item.boundary.receipt_id for item in self.boundaries))
                != self.settlement.crossing_digest):
            raise ValueError("stale generation crossing disagrees with the writer's witness")

    @property
    def source_digest(self) -> str:
        parts = (
            self.stale_path.source_digest, self.complete_receipts.source_digest,
            tuple((item.receipt_source.source_digest, item.accepted_rowid, item.accepted_fields)
                  for item in self.boundaries),
        )
        return hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class UnmatchedLegacyStaleProductivityGenerationSources:
    command_id: str
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.command_id, "unmatched stale generation command")
        if not self.reason:
            raise ValueError("unmatched stale generation requires a reason")


def _accepted_generation_rows(
    db: sqlite3.Connection, repository: str, targets: set[tuple[bytes, bytes]],
) -> dict[tuple[bytes, bytes], int]:
    """Resolve text and BLOB aliases with bounded indexed candidate reads."""
    rows_by_target: dict[tuple[bytes, bytes], int] = {}
    repository_bytes = repository.encode()
    scope_limit = max(len(repository_bytes) + 64, 64)
    candidates = 0
    for namespace in (b"workflow-request", b"workflow-transition"):
        keys = sorted(key for target_namespace, key in targets if target_namespace == namespace)
        for offset in range(0, len(keys), 400):
            batch = keys[offset:offset + 400]
            key_variants = tuple(value for key in batch for value in (key.decode("utf-8"), key))
            marks = ",".join("?" for _ in key_variants)
            cursor = db.execute(
                "SELECT rowid,"
                "CAST(namespace AS BLOB) AS namespace_bytes,"
                "CAST(key AS BLOB) AS key_bytes,"
                "CASE WHEN length(CAST(scope AS BLOB))<=? THEN CAST(scope AS BLOB) END AS scope_bytes "
                f"FROM dedupe_keys WHERE namespace IN (?,?) AND key IN ({marks})",
                (scope_limit, namespace.decode(), namespace, *key_variants),
            )
            while rows := cursor.fetchmany(64):
                candidates += len(rows)
                if candidates > MAX_LEGACY_GENERATION_RECEIPT_SCAN_ROWS:
                    raise ValueError("accepted generation transitions exceed their candidate bound")
                for row in rows:
                    target = (row["namespace_bytes"], row["key_bytes"])
                    if target not in targets:
                        continue
                    scope = row["scope_bytes"]
                    if scope is None:
                        raise ValueError("accepted generation transition scope exceeds its attribution bound")
                    if scope == repository_bytes:
                        if target in rows_by_target:
                            raise ValueError("generation boundary has multiple accepted transitions")
                        rows_by_target[target] = row["rowid"]
                        continue
                    try:
                        alias = canonical_workflow_repository(scope.decode("utf-8"))
                    except (UnicodeError, ValueError):
                        continue
                    if alias == repository:
                        raise ValueError("accepted generation transition uses a repository alias")
    if set(rows_by_target) != targets:
        raise ValueError("generation boundary lacks one exact accepted transition")
    return rows_by_target


_GenerationEpisodeKey = tuple[int, str, FamilyAuthority, str, WorkflowKey, int]


def _generation_episode_key(binding: ReviewExecutionBinding, generation: int) -> _GenerationEpisodeKey:
    return (binding.execution_epoch, binding.execution_episode, binding.rejection.authority,
            binding.rejection.episode_id, binding.rejection.fence.key, generation)


def _indexed_generation_sources(
    complete: CompleteLegacyReviewGenerationReceipts,
) -> dict[_GenerationEpisodeKey, tuple[LegacyReviewGenerationReceiptSource, ...]]:
    indexed: dict[_GenerationEpisodeKey, list[LegacyReviewGenerationReceiptSource]] = {}
    for source in complete.receipts:
        captured = decode_workflow(dict(source.fields)["payload_json"], tuple)
        if (len(captured) != 4 or not isinstance(captured[0], ReviewExecutionGenerationBoundary)
                or any(not isinstance(value, str) for value in captured[1:])):
            raise ValueError("generation receipt has malformed accepted transition fields")
        boundary = captured[0]
        key = _generation_episode_key(boundary.before.binding, boundary.from_revision.fence.generation)
        indexed.setdefault(key, []).append(source)
    return {key: tuple(sources) for key, sources in indexed.items()}


def match_legacy_stale_productivity_generation_sources(
    db: sqlite3.Connection, observed_storage: CompleteLegacySubmissionSettlementStorage,
    observed_generation: CompleteLegacyReviewGenerationReceipts, command_id: str,
) -> MatchedLegacyStaleProductivityGenerationSources | UnmatchedLegacyStaleProductivityGenerationSources:
    """Recheck settlement and generation sets in one snapshot, then join the crossing."""
    validate_workflow_identifier(command_id, "stale generation command")
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("stale generation join requires an explicit SQL transaction")
    if (not isinstance(observed_storage, CompleteLegacySubmissionSettlementStorage)
            or not isinstance(observed_generation, CompleteLegacyReviewGenerationReceipts)):
        raise ValueError("stale generation join requires complete typed observations")
    if observed_storage.repository != observed_generation.repository:
        return UnmatchedLegacyStaleProductivityGenerationSources(command_id, "generation repository disagrees")
    stale_path = verify_legacy_stale_productivity_receipt_path(db, observed_storage, command_id)
    if isinstance(stale_path, UnverifiedLegacyStaleProductivityReceiptPath):
        return UnmatchedLegacyStaleProductivityGenerationSources(command_id, stale_path.reason)
    complete = revalidate_complete_legacy_review_generation_receipts(db, observed_generation)
    if isinstance(complete, BlockedLegacyReviewGenerationReceipts):
        return UnmatchedLegacyStaleProductivityGenerationSources(command_id, complete.reason)
    return _match_stale_productivity_generation_from_sources(db, stale_path, complete)


def _match_stale_productivity_generation_from_sources(
    db: sqlite3.Connection, stale_path: VerifiedLegacyStaleProductivityReceiptPath,
    complete: CompleteLegacyReviewGenerationReceipts,
    *, generation_index: Mapping[_GenerationEpisodeKey, tuple[LegacyReviewGenerationReceiptSource, ...]] | None = None,
    accepted_cache: dict[str, MatchedLegacyReviewGenerationBoundarySources] | None = None,
    workflow_cache: dict[WorkflowKey, ReviewWorkflowSnapshot | None] | None = None,
) -> MatchedLegacyStaleProductivityGenerationSources | UnmatchedLegacyStaleProductivityGenerationSources:
    command_id = dict(stale_path.registered.command_source.command_fields)["command_id"]
    try:
        settlement = decode_workflow(dict(stale_path.stale_receipt.fields)["payload_json"],
                                     tuple | StaleReviewProductivitySettlement)
        if not isinstance(settlement, StaleReviewProductivitySettlement):
            raise ValueError("legacy stale receipt has no generation crossing witness")
        round_id = stale_path.registered.plan.round
        workflow_key = round_id.binding.rejection.fence.key
        if workflow_cache is None:
            current = load_review_workflow_row(db, workflow_key)
        else:
            if workflow_key not in workflow_cache:
                workflow_cache[workflow_key] = load_review_workflow_row(db, workflow_key)
            current = workflow_cache[workflow_key]
        if (current is None or not isinstance(current.execution, ActivatedReviewExecution)
                or not same_execution_episode(round_id.binding, current.execution.binding)
                or settlement.settled_revision.version > current.revision.version
                or settlement.settled_revision.fence.generation > current.revision.fence.generation):
            raise ValueError("stale generation witness exceeds current workflow authority")
        first = round_id.binding.rejection.fence.generation
        last = settlement.settled_revision.fence.generation
        if last - first > MAX_EXECUTION_SUFFIX:
            raise ValueError("stale generation crossing exceeds its suffix bound")
        selected: dict[int, tuple[LegacyReviewGenerationReceiptSource, ReviewExecutionGenerationBoundary,
                                  str, str, str]] = {}
        sources = complete.receipts if generation_index is None else tuple(
            source for generation in range(first, last)
            for source in generation_index.get(_generation_episode_key(round_id.binding, generation), ())
        )
        for source in sources:
            captured = decode_workflow(dict(source.fields)["payload_json"], tuple)
            if (len(captured) != 4 or not isinstance(captured[0], ReviewExecutionGenerationBoundary)
                    or any(not isinstance(value, str) for value in captured[1:])):
                raise ValueError("generation receipt has malformed accepted transition fields")
            boundary, namespace, key, accepted_payload = captured
            if (not same_execution_episode(round_id.binding, boundary.before.binding)
                    or not first <= boundary.from_revision.fence.generation < last):
                continue
            generation = boundary.from_revision.fence.generation
            if generation in selected:
                raise ValueError("stale generation crossing repeats a boundary")
            selected[generation] = (source, boundary, namespace, key, accepted_payload)
        if len(selected) != last - first:
            raise ValueError("stale generation crossing lacks an intervening boundary")
        ordered = tuple(selected[generation] for generation in range(first, last))
        if any(namespace not in {"workflow-request", "workflow-transition"}
               for _, _, namespace, _, _ in ordered):
            raise ValueError("generation receipt lacks an accepted transition namespace")
        all_targets = {(namespace.encode(), key.encode()) for _, _, namespace, key, _ in ordered}
        if len(all_targets) != len(ordered):
            raise ValueError("stale generation crossing repeats an accepted transition")
        targets = {(namespace.encode(), key.encode()) for _, boundary, namespace, key, _ in ordered
                   if accepted_cache is None or boundary.receipt_id not in accepted_cache}
        accepted_rowids = _accepted_generation_rows(db, complete.repository, targets) if targets else {}
        matched: list[MatchedLegacyReviewGenerationBoundarySources] = []
        for source, boundary, namespace, key, accepted_payload in ordered:
            cached = accepted_cache.get(boundary.receipt_id) if accepted_cache is not None else None
            if cached is not None:
                if (cached.complete_receipts != complete or cached.receipt_source != source
                        or cached.boundary != boundary
                        or dict(cached.accepted_fields)["payload_json"] != accepted_payload):
                    raise ValueError("cached generation boundary disagrees with current sources")
                matched.append(cached)
                continue
            accepted_fields, _ = _read_recording_source(db, namespace, complete.repository, key)
            if dict(accepted_fields)["payload_json"] != accepted_payload:
                raise ValueError("accepted generation transition changed")
            proof = MatchedLegacyReviewGenerationBoundarySources(
                complete, source, boundary, accepted_rowids[(namespace.encode(), key.encode())],
                accepted_fields,
            )
            matched.append(proof)
            if accepted_cache is not None:
                accepted_cache[boundary.receipt_id] = proof
        return MatchedLegacyStaleProductivityGenerationSources(stale_path, settlement, complete, tuple(matched))
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, WorkflowAuthorityError) as exc:
        return UnmatchedLegacyStaleProductivityGenerationSources(command_id, _printable(str(exc)))


@dataclass(frozen=True)
class ClassifiedLegacyPlainReviewSuccess:
    source: LegacyReviewSubmissionSource
    proof: VerifiedLegacyPlainProductiveRound

    def __post_init__(self) -> None:
        if (not isinstance(self.source, LegacyReviewSubmissionSource)
                or not isinstance(self.proof, VerifiedLegacyPlainProductiveRound)
                or self.proof.link.source != self.source):
            raise ValueError("plain review classification requires its exact command proof")


@dataclass(frozen=True)
class ClassifiedLegacyEscalatedReviewSuccess:
    source: LegacyReviewSubmissionSource
    proof: VerifiedLegacyEscalatedProductiveRound

    def __post_init__(self) -> None:
        if (not isinstance(self.source, LegacyReviewSubmissionSource)
                or not isinstance(self.proof, VerifiedLegacyEscalatedProductiveRound)
                or self.proof.link.source != self.source):
            raise ValueError("escalated review classification requires its exact command proof")


@dataclass(frozen=True)
class ClassifiedLegacyStaleReviewSuccess:
    source: LegacyReviewSubmissionSource
    proof: MatchedLegacyStaleProductivityGenerationSources

    def __post_init__(self) -> None:
        if (not isinstance(self.source, LegacyReviewSubmissionSource)
                or not isinstance(self.proof, MatchedLegacyStaleProductivityGenerationSources)
                or self.proof.stale_path.registered.command_source != self.source):
            raise ValueError("stale review classification requires its exact command proof")


@dataclass(frozen=True)
class VerifiedLegacyCeilingProductiveRound:
    """Accepted ceiling gate and confirmed round; never a reviewed baseline."""

    complete_storage: CompleteLegacyRoundOutcomeStorage
    storage: VerifiedLegacyRoundOutcomeStorage
    link: VerifiedReviewSubmissionRoundLink
    decision: CeilingProductivityDecision
    receipt: LegacyReviewOutcomeReceiptSource
    receipt_digest: str
    transition_digest: str
    observation_digest: str
    projection_digest: str
    effect_receipt_id: str

    def __post_init__(self) -> None:
        disposition = self.storage.outcome.disposition
        confirmed = tuple(item for item in self.storage.facts.submissions
                          if isinstance(item.result, ReviewSubmissionConfirmed))
        if (self.complete_storage._sealed_by_id.get(self.link.round.round_id) != self.storage
                or self.storage.outcome.round != self.link.round
                or self.complete_storage._receipt_by_key.get(
                    ("review-round-ceiling-productivity", self.link.round.round_id)) != self.receipt
                or bool(self.complete_storage._receipt_names_by_round.get(
                    self.link.round.round_id, frozenset())
                    & {"review-round-stale-productivity", "review-round-superseded-productivity"})
                or self.decision.refusal is not None
                or self.decision.escalation.submission.binding != self.link.round.binding.rejection
                or str(self.decision.escalation.submission.command_id) != dict(self.link.source.command_fields)["command_id"]
                or not isinstance(disposition, (PaidReviewProgress, CopiedReviewProgress))
                or len(confirmed) != 1
                or confirmed[0].result.receipt_id != self.effect_receipt_id
                or disposition.submission_receipt != self.effect_receipt_id
                or str(confirmed[0].plan.command_id) != str(self.decision.escalation.submission.command_id)
                or confirmed[0].plan.kind is not (
                    ReviewSubmissionKind.PAID if isinstance(disposition, PaidReviewProgress)
                    else ReviewSubmissionKind.COPIED
                )):
            raise ValueError("ceiling productivity disagrees with its confirmed round")
        for digest in (self.receipt_digest, self.transition_digest,
                       self.observation_digest, self.projection_digest):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("ceiling productivity requires bounded source digests")


def _verify_legacy_ceiling_productive_round(
    db: sqlite3.Connection, storage: CompleteLegacySubmissionSettlementStorage,
    source: LegacyReviewSubmissionSource, lineage: LegacyReviewOwnerLineage,
    link: VerifiedReviewSubmissionRoundLink,
) -> VerifiedLegacyCeilingProductiveRound:
    command_id = dict(source.command_fields)["command_id"]
    bound = _verified_successful_review_submission(db, source, lineage, storage._unique_remote_review_ids)
    if not isinstance(bound, CeilingEscalation) or bound.escalate_cause != "ceiling":
        raise ValueError("submission is not a bound ceiling escalation")
    round_storage = storage.round_storage._sealed_by_id.get(link.round.round_id)
    receipt = storage.round_storage._receipt_by_key.get(
        ("review-round-ceiling-productivity", link.round.round_id),
    )
    if round_storage is None or receipt is None:
        raise ValueError("ceiling escalation lacks a sealed productive round and decision")
    fields, receipt_digest = _read_recording_source(
        db, "review-round-ceiling-productivity", lineage.repository, link.round.round_id,
    )
    if fields != receipt.fields or dict(fields)["reusable_after"] is not None:
        raise ValueError("ceiling decision changed after inventory or is reclaimable")
    decision, settled_revision, settled_stamp = decode_workflow(dict(fields)["payload_json"], tuple)
    if (not isinstance(decision, CeilingProductivityDecision) or decision.escalation != bound
            or not isinstance(settled_revision, WorkflowRevision) or not isinstance(settled_stamp, str)
            or _parse_stamp(settled_stamp) != _parse_stamp(dict(fields)["claimed_at"])
            or settled_revision.fence != link.round.binding.rejection.fence
            or decision.executor_identity not in lineage.owners
            or decision.prior.binding != bound.submission.binding
            or decision.prior.revision.fence != settled_revision.fence):
        raise ValueError("ceiling decision disagrees with its escalation or round")
    settled_at = _parse_stamp(settled_stamp)
    evidence_columns = (
        "evidence_id", "repository", "target_kind", "target_number", "command_id", "kind", "payload_json",
    )
    evidence = _bounded_identity_row(
        db, "workflow_evidence", "evidence_id", decision.observation_evidence_id,
        evidence_columns, MAX_LEGACY_RECORDING_BYTES,
    )
    key = bound.submission.binding.fence.key
    if (not isinstance(key.target, PullRequestRef)
            or (evidence["repository"], evidence["target_kind"], evidence["target_number"],
                evidence["command_id"], evidence["kind"])
            != (lineage.repository, "pr", key.target.number, None, "observation")
            or type(evidence["payload_json"]) is not str):
        raise ValueError("ceiling decision lacks its exact pull request observation")
    observed = decode_workflow(evidence["payload_json"], RecordedPullRequest)
    if (decision.observation_evidence_id
            != "pr-observation:" + hashlib.sha256(encode_workflow(observed).encode()).hexdigest()
            or observed.observation.key != key
            or not 0 <= (decision.decided_at - observed.observation.observed_at).total_seconds()
            <= OBSERVATION_MAX_AGE_SECONDS
            or not 0 <= (settled_at - observed.observation.observed_at).total_seconds()
            <= OBSERVATION_MAX_AGE_SECONDS):
        raise ValueError("ceiling decision observation is stale or changed")
    proposal = open_review_ceiling(
        decision.prior, observed.observation, now=decision.decided_at,
        rule=decision.configured_rule, ceiling=decision.configured_ceiling,
        escalation=bound, trigger_label=decision.trigger_label,
        executor_identity=decision.executor_identity,
        evidence_ids=(decision.observation_evidence_id,),
    )
    if decision.refusal is not None or not isinstance(proposal, WorkflowChanged):
        raise ValueError("ceiling decision lacks an accepted gate transition")
    transition_key = hashlib.sha256(encode_workflow(decision.prior.revision).encode()).hexdigest()
    transition_fields, transition_digest = _read_recording_source(
        db, "workflow-transition", lineage.repository, transition_key,
    )
    _parse_stamp(dict(transition_fields)["claimed_at"])
    accepted = json.loads(dict(transition_fields)["payload_json"],
                          object_pairs_hook=_unique_receipt_object, parse_constant=_reject_nonfinite)
    if (dict(transition_fields)["reusable_after"] is not None
            or not isinstance(accepted, dict)
            or set(accepted) != {"mutation", "expected", "snapshot", "audit", "commands"}
            or accepted["mutation"] != WorkflowMutation.OPEN_GATE.value
            or decode_workflow(accepted["expected"], WorkflowRevision) != proposal.expected
            or decode_workflow(accepted["snapshot"], ReviewWorkflowSnapshot) != proposal.snapshot
            or decode_workflow(accepted["audit"], ObservedTransition) != proposal.audit
            or not isinstance(accepted["commands"], list)
            or len(accepted["commands"]) != 1
            or settled_revision != proposal.snapshot.revision):
        raise ValueError("accepted ceiling transition disagrees with its decision")
    projected = accepted["commands"][0]
    if not isinstance(projected, dict) or not isinstance(projected.get("id"), str):
        raise ValueError("accepted ceiling transition lacks its hold command")
    projection_row = _bounded_identity_row(
        db, "lifecycle_commands", "command_id", projected["id"],
        LEGACY_SUBMISSION_COMMAND_COLUMNS, MAX_LEGACY_SQL_TEXT_BYTES,
    )
    projection = _command_from_row(projection_row)
    expected_projection = proposal.commands[0]
    if (projection != expected_projection
            or projection_row["logical_key"] != projection.idempotency_key
            or projection_row["occurrence"] != 0
            or projection_row["payload_digest"] != _encoded_payload_digest(
                projection_row["payload_json"], projection_row["workflow_binding_json"],
            )
            or projected != {
                "id": projection.command_id, "idempotency": projection.idempotency_key,
                "kind": projection.kind.value, "target": projection.target,
                "revision": projection.expected_revision, "payload": dict(projection.payload),
                "config": projection.config_version, "repository": projection.repository,
                "owner": projection.executor_identity, "workflow": encode_workflow(projection.workflow),
            }):
        raise ValueError("durable ceiling hold disagrees with its accepted plan")
    effect = dict(source.receipt_fields) if source.receipt_fields is not None else {}
    observation_digest = hashlib.sha256(json.dumps(
        (evidence["source_rowid"], tuple((name, evidence[name]) for name in evidence_columns)),
        separators=(",", ":"),
    ).encode()).hexdigest()
    projection_digest = hashlib.sha256(json.dumps(
        (projection_row["source_rowid"], tuple((name, projection_row[name])
                                          for name in LEGACY_SUBMISSION_COMMAND_COLUMNS)),
        separators=(",", ":"),
    ).encode()).hexdigest()
    return VerifiedLegacyCeilingProductiveRound(
        storage.round_storage, round_storage, link, decision, receipt,
        receipt_digest, transition_digest,
        observation_digest, projection_digest,
        _effect_receipt_id(command_id, effect["receipt_digest"]),
    )


@dataclass(frozen=True)
class ClassifiedLegacyCeilingReviewSuccess:
    source: LegacyReviewSubmissionSource
    proof: VerifiedLegacyCeilingProductiveRound

    def __post_init__(self) -> None:
        if self.proof.link.source != self.source:
            raise ValueError("ceiling review classification requires its exact command proof")


@dataclass(frozen=True)
class UnresolvedLegacyReviewSuccess:
    source: LegacyReviewSubmissionSource
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, LegacyReviewSubmissionSource):
            raise ValueError("unresolved review classification requires a command source")
        validate_workflow_text(self.reason, "unresolved successful review reason")


LegacyReviewSuccessClassification = (
    ClassifiedLegacyPlainReviewSuccess | ClassifiedLegacyEscalatedReviewSuccess
    | ClassifiedLegacyStaleReviewSuccess | ClassifiedLegacyCeilingReviewSuccess
    | UnresolvedLegacyReviewSuccess
)


@dataclass(frozen=True)
class CompleteLegacyReviewSuccessClassifications:
    """Every potentially successful command has one observational disposition."""

    storage: CompleteLegacySubmissionSettlementStorage
    generation: CompleteLegacyReviewGenerationReceipts
    lineage: LegacyReviewOwnerLineage
    results: tuple[LegacyReviewSuccessClassification, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.storage, CompleteLegacySubmissionSettlementStorage)
                or not isinstance(self.generation, CompleteLegacyReviewGenerationReceipts)
                or not isinstance(self.lineage, LegacyReviewOwnerLineage)
                or not isinstance(self.results, tuple)
                or self.storage.repository != self.generation.repository
                or self.storage.repository != self.lineage.repository):
            raise ValueError("complete review classification requires matching typed source sets")
        candidates = tuple(source for source in self.storage.commands.submissions
                           if potentially_successful_review_submission(source))
        if (len(self.results) != len(candidates)
                or any(not isinstance(result, (
                    ClassifiedLegacyPlainReviewSuccess, ClassifiedLegacyEscalatedReviewSuccess,
                    ClassifiedLegacyStaleReviewSuccess, ClassifiedLegacyCeilingReviewSuccess,
                    UnresolvedLegacyReviewSuccess,
                )) or result.source != source
                       for result, source in zip(self.results, candidates, strict=True))):
            raise ValueError("successful review classification leaves a command unaccounted for")
        registered_ids = {dict(item.command_source.command_fields)["command_id"] for item in self.storage.registered}
        marker_ids = {dict(item.fields)["key"] for item in self.storage.recorded_markers}
        for result in self.results:
            command = dict(result.source.command_fields)
            effect = dict(result.source.receipt_fields) if result.source.receipt_fields is not None else {}
            command_id = command["command_id"]
            if (not isinstance(result, UnresolvedLegacyReviewSuccess)
                    and (command["state"] != "succeeded" or effect.get("status") != "succeeded"
                         or command["executor_identity"] not in self.lineage.owners)):
                raise ValueError("proven review success disagrees with its command, effect or owner")
            if isinstance(result, ClassifiedLegacyPlainReviewSuccess):
                if (command_id not in registered_ids or command_id not in marker_ids
                        or result.proof.complete_storage != self.storage.round_storage
                        or result.proof.link.lineage != self.lineage):
                    raise ValueError("plain review classification lacks complete source membership")
            elif isinstance(result, ClassifiedLegacyEscalatedReviewSuccess):
                if (command_id not in registered_ids or command_id not in marker_ids
                        or result.proof.complete_storage != self.storage.round_storage
                        or result.proof.link.lineage != self.lineage):
                    raise ValueError("escalated review classification lacks complete source membership")
            elif isinstance(result, ClassifiedLegacyStaleReviewSuccess):
                if (command_id not in registered_ids or command_id in marker_ids
                        or result.proof.stale_path.complete_storage != self.storage
                        or result.proof.complete_receipts != self.generation):
                    raise ValueError("stale review classification lacks complete source membership")
            elif isinstance(result, ClassifiedLegacyCeilingReviewSuccess):
                if (command_id not in registered_ids or command_id in marker_ids
                        or result.proof.complete_storage != self.storage.round_storage
                        or result.proof.link.lineage != self.lineage):
                    raise ValueError("ceiling review classification lacks complete source membership")

    @property
    def unresolved(self) -> tuple[UnresolvedLegacyReviewSuccess, ...]:
        return tuple(result for result in self.results if isinstance(result, UnresolvedLegacyReviewSuccess))


@dataclass(frozen=True)
class BlockedLegacyReviewSuccessClassifications:
    repository: str
    reason: str

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("blocked review classification requires a canonical repository")
        validate_workflow_text(self.reason, "blocked review classification reason")


def classify_complete_legacy_review_successes(
    db: sqlite3.Connection, observed_storage: CompleteLegacySubmissionSettlementStorage,
    observed_generation: CompleteLegacyReviewGenerationReceipts, observed_lineage: LegacyReviewOwnerLineage,
) -> CompleteLegacyReviewSuccessClassifications | BlockedLegacyReviewSuccessClassifications:
    """Account for every success claim after rechecking all source sets in one snapshot."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("review classification requires an explicit SQL transaction")
    if (not isinstance(observed_storage, CompleteLegacySubmissionSettlementStorage)
            or not isinstance(observed_generation, CompleteLegacyReviewGenerationReceipts)
            or not isinstance(observed_lineage, LegacyReviewOwnerLineage)):
        raise ValueError("review classification requires complete typed observations")
    repository = observed_storage.repository
    if repository != observed_generation.repository or repository != observed_lineage.repository:
        return BlockedLegacyReviewSuccessClassifications(repository, "review classification repositories disagree")
    storage = verify_complete_legacy_submission_settlement_storage(
        db, observed_storage.commands, observed_storage.receipts, observed_storage.round_storage,
    )
    if isinstance(storage, BlockedLegacySubmissionSettlementStorage):
        return BlockedLegacyReviewSuccessClassifications(repository, storage.reason)
    generation = revalidate_complete_legacy_review_generation_receipts(db, observed_generation)
    if isinstance(generation, BlockedLegacyReviewGenerationReceipts):
        return BlockedLegacyReviewSuccessClassifications(repository, generation.reason)
    lineage = read_legacy_review_owner_lineage(
        db, repository, observed_lineage.current_owner,
        configured_prior_owners=observed_lineage.configured_prior_owners,
    )
    if lineage != observed_lineage:
        return BlockedLegacyReviewSuccessClassifications(repository, "review owner lineage changed after inventory")
    generation_index: dict[_GenerationEpisodeKey, tuple[LegacyReviewGenerationReceiptSource, ...]] | None = None
    accepted_cache: dict[str, MatchedLegacyReviewGenerationBoundarySources] = {}
    workflow_cache: dict[WorkflowKey, ReviewWorkflowSnapshot | None] = {}
    results: list[LegacyReviewSuccessClassification] = []
    for source in storage.commands.submissions:
        if not potentially_successful_review_submission(source):
            continue
        command = dict(source.command_fields)
        command_id = command["command_id"]
        effect = dict(source.receipt_fields) if source.receipt_fields is not None else {}
        registration = storage._registered_by_id.get(command_id)
        if command["state"] != "succeeded" or effect.get("status") != "succeeded":
            results.append(UnresolvedLegacyReviewSuccess(source, "command and effect do not both confirm success"))
        elif registration is None:
            results.append(UnresolvedLegacyReviewSuccess(source, "successful historical command lacks a round registration"))
        elif command["executor_identity"] not in lineage.owners:
            results.append(UnresolvedLegacyReviewSuccess(source, "successful command owner is outside the review lineage"))
        elif command_id in storage._recorded_marker_ids:
            link = read_review_submission_round_link(db, source, lineage)
            if isinstance(link, UnverifiedReviewSubmissionRoundLink):
                results.append(UnresolvedLegacyReviewSuccess(source, link.reason))
                continue
            round_storage = storage.round_storage._sealed_by_id.get(registration.plan.round.round_id)
            if round_storage is None:
                results.append(UnresolvedLegacyReviewSuccess(source, "recorded review lacks a sealed round"))
                continue
            try:
                bound = load_bound_review_submission(db, CommandId(command_id))
                if isinstance(bound, ReviewSubmissionReceipt) and bound.escalated:
                    hold = _match_legacy_escalated_review_hold_plan(
                        db, source, lineage, storage._unique_remote_review_ids,
                    )
                    if isinstance(hold, UnmatchedLegacyEscalatedReviewHoldPlan):
                        raise ValueError(hold.reason)
                    proof = VerifiedLegacyEscalatedProductiveRound(
                        storage.round_storage, round_storage, link, hold,
                        _effect_receipt_id(command_id, effect["receipt_digest"]),
                    )
                    results.append(ClassifiedLegacyEscalatedReviewSuccess(source, proof))
                else:
                    baseline = _read_legacy_recorded_review(
                        db, source, lineage, storage._unique_remote_review_ids,
                    )
                    if isinstance(baseline, UnverifiedLegacyRecordedReview):
                        raise ValueError(baseline.reason)
                    proof = VerifiedLegacyPlainProductiveRound(
                        storage.round_storage, round_storage, link, baseline,
                        _effect_receipt_id(command_id, effect["receipt_digest"]),
                    )
                    results.append(ClassifiedLegacyPlainReviewSuccess(source, proof))
            except (KeyError, TypeError, ValueError, WorkflowAuthorityError, CorruptControlStore) as exc:
                results.append(UnresolvedLegacyReviewSuccess(source, _printable(str(exc))))
        elif ("review-round-ceiling-productivity", registration.plan.round.round_id) in (
            storage.round_storage._receipt_by_key
        ):
            link = read_review_submission_round_link(db, source, lineage)
            if isinstance(link, UnverifiedReviewSubmissionRoundLink):
                results.append(UnresolvedLegacyReviewSuccess(source, link.reason))
                continue
            try:
                proof = _verify_legacy_ceiling_productive_round(db, storage, source, lineage, link)
                results.append(ClassifiedLegacyCeilingReviewSuccess(source, proof))
            except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, sqlite3.Error,
                    WorkflowAuthorityError, CorruptControlStore) as exc:
                results.append(UnresolvedLegacyReviewSuccess(source, _printable(str(exc))))
        else:
            path = _stale_productivity_path_from_storage(storage, command_id)
            if isinstance(path, UnverifiedLegacyStaleProductivityReceiptPath):
                results.append(UnresolvedLegacyReviewSuccess(source, path.reason))
                continue
            if generation_index is None:
                try:
                    generation_index = _indexed_generation_sources(generation)
                except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
                    return BlockedLegacyReviewSuccessClassifications(repository, _printable(str(exc)))
            matched = _match_stale_productivity_generation_from_sources(
                db, path, generation, generation_index=generation_index,
                accepted_cache=accepted_cache, workflow_cache=workflow_cache,
            )
            if isinstance(matched, UnmatchedLegacyStaleProductivityGenerationSources):
                results.append(UnresolvedLegacyReviewSuccess(source, matched.reason))
                continue
            results.append(ClassifiedLegacyStaleReviewSuccess(source, matched))
    try:
        return CompleteLegacyReviewSuccessClassifications(storage, generation, lineage, tuple(results))
    except ValueError as exc:
        return BlockedLegacyReviewSuccessClassifications(repository, _printable(str(exc)))


_ExecutionEpisodeKey = tuple[WorkflowKey, int, str]


def _execution_episode_key(binding: ReviewExecutionBinding) -> _ExecutionEpisodeKey:
    return (binding.rejection.fence.key, binding.execution_epoch, binding.execution_episode)


@dataclass(frozen=True)
class MatchedLegacyAllocationGenerationSources:
    """Accepted generation boundaries assign every allocated round a binding."""

    round_storage: CompleteLegacyRoundOutcomeStorage
    generation: CompleteLegacyReviewGenerationReceipts
    workflow: ReviewWorkflowSnapshot
    rounds: tuple[ReviewRoundIdentity, ...]
    boundaries: tuple[MatchedLegacyReviewGenerationBoundarySources, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.round_storage, CompleteLegacyRoundOutcomeStorage)
                or not isinstance(self.generation, CompleteLegacyReviewGenerationReceipts)
                or not isinstance(self.workflow, ReviewWorkflowSnapshot)
                or not isinstance(self.workflow.execution, ActivatedReviewExecution)
                or not isinstance(self.rounds, tuple) or not self.rounds
                or len(self.rounds) > MAX_EXECUTION_SUFFIX
                or any(not isinstance(round_, ReviewRoundIdentity) for round_ in self.rounds)
                or not isinstance(self.boundaries, tuple)
                or any(not isinstance(item, MatchedLegacyReviewGenerationBoundarySources)
                       for item in self.boundaries)):
            raise ValueError("allocation generation proof requires bounded typed sources")
        execution = self.workflow.execution
        first = self.rounds[0]
        if (self.round_storage.repository != self.generation.repository
                or self.round_storage.repository != first.binding.rejection.fence.key.repository
                or self.workflow.revision.fence.key != first.binding.rejection.fence.key
                or not same_execution_episode(first.binding, execution.binding)
                or execution.checkpoint.through_sequence != 0
                or execution.checkpoint.boundary_cursor is not None
                or execution.last_sequence != len(self.rounds)
                or tuple(round_.sequence for round_ in self.rounds) != tuple(range(1, len(self.rounds) + 1))
                or len({round_.round_id for round_ in self.rounds}) != len(self.rounds)
                or any(not same_execution_episode(first.binding, round_.binding) for round_ in self.rounds)
                or tuple(item.boundary for item in self.boundaries) != execution.boundaries
                or any(item.complete_receipts is not self.generation for item in self.boundaries)):
            raise ValueError("allocation generations have a gap or crossed execution")
        active = execution.checkpoint.state.binding
        boundary_index = 0
        for round_ in self.rounds:
            while (boundary_index < len(execution.boundaries)
                   and round_.sequence > execution.boundaries[boundary_index].through_sequence):
                boundary = execution.boundaries[boundary_index]
                if boundary.before.binding != active:
                    raise ValueError("allocation generation chain disagrees with its predecessor")
                active = boundary.after.binding
                boundary_index += 1
            if round_.binding != active:
                raise ValueError("allocated review round uses an unaccepted generation")
        while boundary_index < len(execution.boundaries):
            boundary = execution.boundaries[boundary_index]
            if boundary.through_sequence != len(self.rounds) or boundary.before.binding != active:
                raise ValueError("allocation generation chain disagrees with its final boundary")
            active = boundary.after.binding
            boundary_index += 1
        if boundary_index != len(execution.boundaries) or active != execution.binding:
            raise ValueError("allocation generation chain does not reach the current binding")

@dataclass(frozen=True)
class CompleteLegacyAllocationGenerationSources:
    round_storage: CompleteLegacyRoundOutcomeStorage
    generation: CompleteLegacyReviewGenerationReceipts
    episodes: tuple[MatchedLegacyAllocationGenerationSources, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.round_storage, CompleteLegacyRoundOutcomeStorage)
                or not isinstance(self.generation, CompleteLegacyReviewGenerationReceipts)
                or not isinstance(self.episodes, tuple)
                or any(not isinstance(item, MatchedLegacyAllocationGenerationSources)
                       or item.round_storage is not self.round_storage or item.generation is not self.generation
                       for item in self.episodes)):
            raise ValueError("complete allocation generations require matching typed sources")
        planned = tuple(_stored_review_round_plan(source).round for source in self.round_storage.rounds.rounds)
        accounted = tuple(round_ for episode in self.episodes for round_ in episode.rounds)
        if (len(accounted) != len(planned)
                or len({round_.round_id for round_ in accounted}) != len(accounted)
                or set(accounted) != set(planned)):
            raise ValueError("allocation generation proof leaves a round unaccounted for")

    @property
    def source_digest(self) -> str:
        parts = (self.round_storage.source_digest, self.generation.source_digest,
                 tuple((encode_workflow(episode.workflow),
                        tuple(round_.round_id for round_ in episode.rounds),
                        tuple((boundary.receipt_source.source_digest, boundary.accepted_rowid,
                               boundary.accepted_fields) for boundary in episode.boundaries))
                       for episode in self.episodes))
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class BlockedLegacyAllocationGenerationSources:
    repository: str
    reason: str

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("blocked allocation generations require a canonical repository")
        validate_workflow_text(self.reason, "blocked allocation generation reason")


_IndexedAllocationBoundary = tuple[
    LegacyReviewGenerationReceiptSource, ReviewExecutionGenerationBoundary, str, str, str,
]


def _indexed_allocation_generation_boundaries(
    complete: CompleteLegacyReviewGenerationReceipts,
) -> dict[_ExecutionEpisodeKey, dict[str, _IndexedAllocationBoundary]]:
    indexed: dict[_ExecutionEpisodeKey, dict[str, _IndexedAllocationBoundary]] = {}
    for source in complete.receipts:
        captured = decode_workflow(dict(source.fields)["payload_json"], tuple)
        if (len(captured) != 4 or not isinstance(captured[0], ReviewExecutionGenerationBoundary)
                or any(not isinstance(value, str) for value in captured[1:])):
            raise ValueError("generation receipt has malformed accepted transition fields")
        boundary, namespace, key, accepted_payload = captured
        related = indexed.setdefault(_execution_episode_key(boundary.before.binding), {})
        if boundary.receipt_id in related:
            raise ValueError("execution generation receipt repeats a boundary")
        related[boundary.receipt_id] = (source, boundary, namespace, key, accepted_payload)
    return indexed


def _matched_allocation_generation_boundaries(
    db: sqlite3.Connection, complete: CompleteLegacyReviewGenerationReceipts,
    execution: ActivatedReviewExecution,
    indexed: Mapping[_ExecutionEpisodeKey, Mapping[str, _IndexedAllocationBoundary]],
) -> tuple[MatchedLegacyReviewGenerationBoundarySources, ...]:
    """Batch accepted-row reads after a complete receipt inventory was rechecked."""
    related = indexed.get(_execution_episode_key(execution.binding), {})
    if set(related) != {boundary.receipt_id for boundary in execution.boundaries}:
        raise ValueError("execution generation receipts disagree with current boundaries")
    selected = tuple(related[boundary.receipt_id] for boundary in execution.boundaries)
    targets = {(namespace.encode(), key.encode()) for _, _, namespace, key, _ in selected}
    if (len(targets) != len(selected)
            or any(namespace not in {"workflow-request", "workflow-transition"}
                   for _, _, namespace, _, _ in selected)):
        raise ValueError("execution generation receipts repeat an accepted transition")
    accepted_rowids = _accepted_generation_rows(db, complete.repository, targets) if targets else {}
    matched: list[MatchedLegacyReviewGenerationBoundarySources] = []
    for source, boundary, namespace, key, accepted_payload in selected:
        accepted_fields, _ = _read_recording_source(db, namespace, complete.repository, key)
        if dict(accepted_fields)["payload_json"] != accepted_payload:
            raise ValueError("accepted generation transition changed")
        matched.append(MatchedLegacyReviewGenerationBoundarySources(
            complete, source, boundary, accepted_rowids[(namespace.encode(), key.encode())],
            accepted_fields,
        ))
    return tuple(matched)


def verify_complete_legacy_allocation_generation_sources(
    db: sqlite3.Connection, observed_round_storage: CompleteLegacyRoundOutcomeStorage,
    observed_generation: CompleteLegacyReviewGenerationReceipts,
) -> CompleteLegacyAllocationGenerationSources | BlockedLegacyAllocationGenerationSources:
    """Recheck complete SQL sets and assign uncompacted rounds to accepted generations."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("allocation generation verification requires an explicit SQL transaction")
    if (not isinstance(observed_round_storage, CompleteLegacyRoundOutcomeStorage)
            or not isinstance(observed_generation, CompleteLegacyReviewGenerationReceipts)):
        raise ValueError("allocation generation verification requires complete typed observations")
    repository = observed_round_storage.repository
    if repository != observed_generation.repository:
        return BlockedLegacyAllocationGenerationSources(repository, "allocation generation repositories disagree")
    storage = verify_complete_legacy_round_outcome_storage(
        db, observed_round_storage.rounds, observed_round_storage.receipts,
    )
    if isinstance(storage, BlockedLegacyRoundOutcomeStorage):
        return BlockedLegacyAllocationGenerationSources(repository, storage.reason)
    generation = revalidate_complete_legacy_review_generation_receipts(db, observed_generation)
    if isinstance(generation, BlockedLegacyReviewGenerationReceipts):
        return BlockedLegacyAllocationGenerationSources(repository, generation.reason)
    grouped: dict[_ExecutionEpisodeKey, list[ReviewRoundIdentity]] = {}
    try:
        generation_index = _indexed_allocation_generation_boundaries(generation)
        for source in storage.rounds.rounds:
            identity = _stored_review_round_plan(source).round
            grouped.setdefault(_execution_episode_key(identity.binding), []).append(identity)
        episodes: list[MatchedLegacyAllocationGenerationSources] = []
        for (workflow_key, _, _), identities in sorted(grouped.items(), key=lambda pair: (
            pair[0][0].repository, pair[0][0].target.number, pair[0][1], pair[0][2],
        )):
            workflow = load_review_workflow_row(db, workflow_key)
            if workflow is None or not isinstance(workflow.execution, ActivatedReviewExecution):
                raise ValueError("allocated review episode lacks current activated workflow")
            matched = _matched_allocation_generation_boundaries(
                db, generation, workflow.execution, generation_index,
            )
            episodes.append(MatchedLegacyAllocationGenerationSources(
                storage, generation, workflow,
                tuple(sorted(identities, key=lambda item: item.sequence)), matched,
            ))
        return CompleteLegacyAllocationGenerationSources(storage, generation, tuple(episodes))
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError,
            WorkflowAuthorityError, CorruptControlStore) as exc:
        return BlockedLegacyAllocationGenerationSources(repository, _printable(str(exc)))


def _classified_productive_storage(
    result: ClassifiedLegacyPlainReviewSuccess | ClassifiedLegacyEscalatedReviewSuccess
    | ClassifiedLegacyStaleReviewSuccess | ClassifiedLegacyCeilingReviewSuccess,
) -> VerifiedLegacyRoundOutcomeStorage:
    return (result.proof.stale_path.sealed if isinstance(result, ClassifiedLegacyStaleReviewSuccess)
            else result.proof.storage)


@dataclass(frozen=True)
class CompleteLegacyOrderedOutcomeCoverage:
    """Every allocated round is ordered and productive outcomes have proofs."""

    classifications: CompleteLegacyReviewSuccessClassifications
    episodes: tuple[LegacyOrderedExecutionEpisode, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.classifications, CompleteLegacyReviewSuccessClassifications)
                or self.classifications.unresolved or not isinstance(self.episodes, tuple)
                or any(not isinstance(episode, LegacyOrderedExecutionEpisode) for episode in self.episodes)):
            raise ValueError("ordered outcome coverage requires resolved complete classifications")
        plans = {plan.round.round_id: plan for _, plan in self.classifications.storage.round_plans}
        sealed = self.classifications.storage.round_storage._sealed_by_id
        entries = tuple(item for episode in self.episodes for item in episode.rounds)
        by_round = {item.round.round_id: item for item in entries}
        episode_keys = [_execution_episode_key(episode.rounds[0].round.binding) for episode in self.episodes]
        if (len(by_round) != len(entries) or set(by_round) != set(plans)
                or len(set(episode_keys)) != len(episode_keys)
                or any(item.round != plans[round_id].round for round_id, item in by_round.items())):
            raise ValueError("ordered outcome coverage leaves an allocated round unaccounted for")
        finalized = {round_id: item for round_id, item in by_round.items()
                     if isinstance(item, (FinalizedLegacyUnproductiveRound, FinalizedLegacyProductiveRound))}
        if (set(finalized) != set(sealed)
                or any(item.outcome != sealed[round_id].outcome for round_id, item in finalized.items())):
            raise ValueError("ordered outcome coverage leaves a sealed outcome unaccounted for")
        proven: dict[str, tuple[str, VerifiedLegacyRoundOutcomeStorage]] = {}
        for result in self.classifications.results:
            if isinstance(result, UnresolvedLegacyReviewSuccess):
                raise ValueError("ordered outcome coverage contains an unresolved success")
            round_storage = _classified_productive_storage(result)
            round_id = round_storage.outcome.round.round_id
            command_id = dict(result.source.command_fields)["command_id"]
            if round_id in proven or sealed.get(round_id) != round_storage:
                raise ValueError("ordered outcome coverage has duplicate or foreign productivity proof")
            proven[round_id] = (command_id, round_storage)
        productive = {round_id: item for round_id, item in finalized.items()
                      if isinstance(item, FinalizedLegacyProductiveRound)}
        if (set(proven) != set(productive)
                or any(proven[round_id][0] != item.command_id for round_id, item in productive.items())):
            raise ValueError("ordered outcome coverage leaves a productive round unproved")

    @property
    def source_digest(self) -> str:
        parts = (self.classifications.storage.source_digest, self.classifications.generation.source_digest,
                 self.classifications.lineage.source_digest,
                 tuple((item.round.round_id, item.command_id)
                       for episode in self.episodes for item in episode.rounds
                       if isinstance(item, FinalizedLegacyProductiveRound)))
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class BlockedLegacyOrderedOutcomeCoverage:
    repository: str
    reason: str

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("blocked ordered outcomes require a canonical repository")
        validate_workflow_text(self.reason, "blocked ordered outcome reason")


def _complete_legacy_ordered_outcomes(
    current: CompleteLegacyReviewSuccessClassifications,
) -> CompleteLegacyOrderedOutcomeCoverage:
    if current.unresolved:
        command_id = dict(current.unresolved[0].source.command_fields)["command_id"]
        raise ValueError(f"successful command {command_id} remains unresolved: {current.unresolved[0].reason}")
    proven: dict[str, tuple[str, VerifiedLegacyRoundOutcomeStorage]] = {}
    for result in current.results:
        round_storage = _classified_productive_storage(result)
        round_id = round_storage.outcome.round.round_id
        command_id = dict(result.source.command_fields)["command_id"]
        if round_id in proven:
            raise ValueError("two successful commands claim one productive round")
        proven[round_id] = (command_id, round_storage)
    grouped: dict[_ExecutionEpisodeKey, list[
        OpenLegacyOrderedRound | FinalizedLegacyUnproductiveRound | FinalizedLegacyProductiveRound,
    ]] = {}
    for _, plan in current.storage.round_plans:
        identity = plan.round
        sealed = current.storage.round_storage._sealed_by_id.get(identity.round_id)
        if sealed is None:
            entry = OpenLegacyOrderedRound(identity)
        elif isinstance(sealed.outcome.disposition, (PaidReviewProgress, CopiedReviewProgress)):
            matched = proven.pop(identity.round_id, None)
            if matched is None or matched[1] != sealed:
                raise ValueError("productive sealed round lacks one classified successful submission")
            entry = FinalizedLegacyProductiveRound(sealed.outcome, CommandId(matched[0]))
        else:
            entry = FinalizedLegacyUnproductiveRound(sealed.outcome)
        grouped.setdefault(_execution_episode_key(identity.binding), []).append(entry)
    if proven:
        raise ValueError("classified success has no productive sealed round")
    episodes = tuple(LegacyOrderedExecutionEpisode(tuple(sorted(
        entries, key=lambda item: item.round.sequence,
    ))) for _, entries in sorted(grouped.items(), key=lambda pair: (
        pair[0][0].repository, pair[0][0].target.number, pair[0][1], pair[0][2],
    )))
    return CompleteLegacyOrderedOutcomeCoverage(current, episodes)


def verify_complete_legacy_ordered_outcome_coverage(
    db: sqlite3.Connection, observed: CompleteLegacyReviewSuccessClassifications,
) -> CompleteLegacyOrderedOutcomeCoverage | BlockedLegacyOrderedOutcomeCoverage:
    """Recheck SQL and pair current-generation rounds before ordered reset replay."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("ordered outcome coverage requires an explicit SQL transaction")
    if not isinstance(observed, CompleteLegacyReviewSuccessClassifications):
        raise ValueError("ordered outcome coverage requires complete typed classifications")
    current = classify_complete_legacy_review_successes(
        db, observed.storage, observed.generation, observed.lineage,
    )
    if isinstance(current, BlockedLegacyReviewSuccessClassifications):
        return BlockedLegacyOrderedOutcomeCoverage(observed.storage.repository, current.reason)
    try:
        coverage = _complete_legacy_ordered_outcomes(current)
        for episode in coverage.episodes:
            first = episode.rounds[0].round.binding
            workflow_key, execution_epoch, execution_episode = _execution_episode_key(first)
            current_workflow = load_review_workflow_row(db, workflow_key)
            if (current_workflow is None
                    or not isinstance(current_workflow.execution, ActivatedReviewExecution)
                    or current_workflow.execution.binding != first
                    or any(item.round.binding != current_workflow.execution.binding for item in episode.rounds)):
                raise ValueError(
                    f"execution {execution_epoch}:{execution_episode} requires accepted historical generation proof"
                )
        return coverage
    except (TypeError, ValueError, KeyError, WorkflowAuthorityError, CorruptControlStore) as exc:
        return BlockedLegacyOrderedOutcomeCoverage(current.storage.repository, _printable(str(exc)))


@dataclass(frozen=True)
class ObservedLegacyOrderedCheckpointReplay:
    """Suffix replay matches persisted state, starting from an unproved checkpoint.

    This observation cannot certify the checkpoint/origin or authorize activation.
    """

    coverage: CompleteLegacyOrderedOutcomeCoverage
    allocations: CompleteLegacyAllocationGenerationSources

    def __post_init__(self) -> None:
        if (not isinstance(self.coverage, CompleteLegacyOrderedOutcomeCoverage)
                or not isinstance(self.allocations, CompleteLegacyAllocationGenerationSources)
                or self.coverage.classifications.storage.round_storage != self.allocations.round_storage
                or self.coverage.classifications.generation != self.allocations.generation
                or len(self.coverage.episodes) != len(self.allocations.episodes)):
            raise ValueError("ordered replay requires matching complete source proofs")
        by_key = {_execution_episode_key(item.rounds[0].binding): item
                  for item in self.allocations.episodes}
        if len(by_key) != len(self.allocations.episodes):
            raise ValueError("ordered replay repeats an execution allocation")
        for episode in self.coverage.episodes:
            allocation = by_key.get(_execution_episode_key(episode.rounds[0].round.binding))
            if (allocation is None
                    or tuple(item.round for item in episode.rounds) != allocation.rounds):
                raise ValueError("ordered replay lacks exact allocation generation proof")
            execution = allocation.workflow.execution
            if not isinstance(execution, ActivatedReviewExecution):
                raise ValueError("ordered replay requires activated execution")
            replayed = replay_review_execution_generations(
                execution.checkpoint, episode.outcomes,
                tuple(item.boundary for item in allocation.boundaries),
            )
            if replayed != execution.state:
                raise ValueError("ordered replay disagrees with current execution projection")

    @property
    def source_digest(self) -> str:
        parts = (self.coverage.source_digest, self.allocations.source_digest)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class BlockedLegacyOrderedCheckpointReplay:
    repository: str
    reason: str

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("blocked ordered replay requires a canonical repository")
        validate_workflow_text(self.reason, "blocked ordered replay reason")


def observe_legacy_ordered_checkpoint_replay(
    db: sqlite3.Connection, observed: CompleteLegacyReviewSuccessClassifications,
    observed_allocations: CompleteLegacyAllocationGenerationSources,
) -> ObservedLegacyOrderedCheckpointReplay | BlockedLegacyOrderedCheckpointReplay:
    """Recheck sources and compare outcomes with persisted state from its unverified checkpoint."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("ordered replay requires an explicit SQL transaction")
    if (not isinstance(observed, CompleteLegacyReviewSuccessClassifications)
            or not isinstance(observed_allocations, CompleteLegacyAllocationGenerationSources)):
        raise ValueError("ordered replay requires complete typed observations")
    repository = observed.storage.repository
    if repository != observed_allocations.round_storage.repository:
        return BlockedLegacyOrderedCheckpointReplay(repository, "ordered replay repositories disagree")
    current = classify_complete_legacy_review_successes(
        db, observed.storage, observed.generation, observed.lineage,
    )
    if isinstance(current, BlockedLegacyReviewSuccessClassifications):
        return BlockedLegacyOrderedCheckpointReplay(repository, current.reason)
    allocations = verify_complete_legacy_allocation_generation_sources(
        db, observed_allocations.round_storage, observed_allocations.generation,
    )
    if isinstance(allocations, BlockedLegacyAllocationGenerationSources):
        return BlockedLegacyOrderedCheckpointReplay(repository, allocations.reason)
    try:
        return ObservedLegacyOrderedCheckpointReplay(_complete_legacy_ordered_outcomes(current), allocations)
    except (KeyError, TypeError, ValueError) as exc:
        return BlockedLegacyOrderedCheckpointReplay(repository, _printable(str(exc)))
