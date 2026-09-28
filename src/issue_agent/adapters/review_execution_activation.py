"""Read-only inventory of legacy review execution files across state roots.

This inventory retains raw observations. Mapping issue counters to PRs and
validating their values require SQL run history and fresh linked-issue evidence.
"""

from __future__ import annotations

import os
import re
import sqlite3

from ..domain.resolve_activation import LegacyAbsent, LegacyFile, UnmappedLegacyEntry
from ..domain.review_execution_activation import (
    MAX_LEGACY_SQL_TEXT_BYTES, MAX_SQL_ROWID, MIN_SQL_ROWID, LegacyExecutionCounterRecord,
    LegacyExecutionEntry, LegacyExecutionPRRecord,
    LegacyExecutionRunPage, LegacyExecutionRunRecord, LegacyReviewRunState, UnreadableLegacyExecutionRun,
    LEGACY_SUBMISSION_COMMAND_COLUMNS, LEGACY_SUBMISSION_RECEIPT_COLUMNS,
    LegacyReviewSubmissionPage, LegacyReviewSubmissionSource, UnreadableLegacyReviewSubmission,
    LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS, LEGACY_REVIEW_SUBMISSION_RECEIPT_NAMESPACES,
    LegacyReviewSubmissionReceiptPage, LegacyReviewSubmissionReceiptSource,
    UnreadableLegacyReviewSubmissionReceipt,
    MAX_LEGACY_REVIEW_OWNERS, LegacyReviewOwnerLink, LegacyReviewOwnerLineage,
    UnreadableLegacyReviewOwnerLineage,
)
from ..domain.workflow_identity import canonical_workflow_repository, validate_workflow_identifier
from .resolve_activation import LegacyRoots, _printable, _read_legacy_file, distinct_roots

_PR_NAME = re.compile(r"pr-(.*)\.(session|round)", re.DOTALL)
_COUNTER_NAME = re.compile(r"issue-(.*)\.infra-deaths", re.DOTALL)
_POSITIVE = re.compile(r"[1-9][0-9]{0,18}")
_SIGNED = re.compile(r"-?[1-9][0-9]{0,18}")
_MAX_NUMBER = 2**63 - 1
MAX_RUN_SOURCE_PAGE = 256
MAX_SUBMISSION_SOURCE_PAGE = 64
MAX_SUBMISSION_RECEIPT_SOURCE_PAGE = 64
_RETIRED_OWNER_COLUMNS = ("owner_id", "successor_owner_id", "repository", "role", "retired_at")
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
    if after_rowid is not None and (type(after_rowid) is not int or not MIN_SQL_ROWID <= after_rowid <= MAX_SQL_ROWID):
        raise ValueError("legacy run scan requires a valid cursor")

    sizes = ",".join(f"ifnull(length(CAST({column} AS BLOB)),0)" for column in _RUN_COLUMNS)
    predicate = "WHERE owner_id=?" + (" AND rowid>?" if after_rowid is not None else "")
    params = (owner_id,) if after_rowid is None else (owner_id, after_rowid)
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
        fields = ",".join(
            f"typeof({column}) AS type_{column},CAST({column} AS BLOB) AS raw_{column}"
            for column in _RUN_COLUMNS
        )
        values = {row["source_rowid"]: row for row in db.execute(
            f"SELECT rowid AS source_rowid,{fields} FROM runs "
            f"WHERE owner_id=? AND rowid IN ({placeholders})",
            (owner_id, *safe_ids),
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
        try:
            fields = {column: _sql_text(source[f"type_{column}"], source[f"raw_{column}"])
                      for column in _RUN_COLUMNS}
            state = LegacyReviewRunState(fields["state"])
            entries.append(LegacyExecutionRunRecord(
                rowid, owner_id, fields["run_id"], fields["dispatch_id"], fields["kind"], state,
                fields["config_version"], fields["started_at"], fields["completed_at"], fields["outcome"],
                fields["provider_receipt_id"], fields["workflow_binding_json"], fields["workflow_episode_key"],
                fields["review_round_id"], fields["review_child_id"],
            ))
        except (TypeError, ValueError, UnicodeError):
            entries.append(UnreadableLegacyExecutionRun(rowid, "run source has malformed fields"))
    return LegacyExecutionRunPage(tuple(entries), page[-1]["source_rowid"] if page else after_rowid,
                                  len(metadata) > MAX_RUN_SOURCE_PAGE)


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
