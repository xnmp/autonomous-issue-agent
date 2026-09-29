"""Global rowid pages for an eventual durable review submission lineage audit."""

from __future__ import annotations

import sqlite3

from ..domain.review_execution_activation import (
    LEGACY_SUBMISSION_COMMAND_COLUMNS, LEGACY_SUBMISSION_RECEIPT_COLUMNS,
    MAX_LEGACY_SQL_TEXT_BYTES, MAX_SQL_ROWID, MIN_SQL_ROWID,
    LegacyReviewSubmissionSource,
)
from ..domain.review_lifecycle_archive import (
    MAX_REVIEW_LIFECYCLE_PAGE_BYTES, MAX_REVIEW_LIFECYCLE_SOURCE_PAGE,
    BlockedReviewLifecycleSourcePage, ObservedReviewLifecycleEffectPage,
    ObservedReviewLifecycleSourcePage, ReviewLifecycleEffectSource,
)
from ..domain.workflow_identity import canonical_workflow_repository
from .review_archive_source_versions import read_review_lifecycle_source_version
from .review_execution_activation import _sql_integer, _sql_text


def _size_expression(field: str) -> str:
    if sqlite3.sqlite_version_info >= (3, 43, 0):
        return f"ifnull(octet_length({field}),0)"
    return f"ifnull(length(CAST({field} AS BLOB)),0)"


def _selection_columns(prefix: str) -> str:
    repository = f"{prefix}repository"
    kind = f"{prefix}kind"
    return (
        f"CASE WHEN {_size_expression(repository)}=? THEN {repository}=? ELSE 0 END "
        "AS repository_text_match,"
        f"CASE WHEN {_size_expression(repository)}=? THEN {repository}=CAST(? AS BLOB) "
        "ELSE 0 END AS repository_blob_match,"
        f"CASE WHEN {_size_expression(kind)}=? THEN {kind}=? ELSE 0 END "
        "AS kind_text_match,"
        f"CASE WHEN {_size_expression(kind)}=? THEN {kind}=CAST(? AS BLOB) "
        "ELSE 0 END AS kind_blob_match"
    )


def _selection_parameters(repository: str) -> tuple[object, ...]:
    repository_bytes = len(repository.encode("utf-8"))
    kind = "submit_review"
    kind_bytes = len(kind)
    return (
        repository_bytes, repository, repository_bytes, repository,
        kind_bytes, kind, kind_bytes, kind,
    )


def _selected_review_source(row: sqlite3.Row) -> bool:
    return ((row["repository_text_match"] == 1 or row["repository_blob_match"] == 1)
            and (row["kind_text_match"] == 1 or row["kind_blob_match"] == 1))


def _selected_alias(row: sqlite3.Row) -> bool:
    return row["repository_blob_match"] == 1 or row["kind_blob_match"] == 1


def _validate_page_request(
    db: sqlite3.Connection, repository: str, after_rowid: int | None,
    upper_rowid: int | None, limit: int, max_source_bytes: int,
) -> None:
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or canonical_workflow_repository(repository) != repository
            or (after_rowid is not None
                and (type(after_rowid) is not int
                     or not MIN_SQL_ROWID <= after_rowid <= MAX_SQL_ROWID))
            or (upper_rowid is not None
                and (type(upper_rowid) is not int
                     or not MIN_SQL_ROWID <= upper_rowid <= MAX_SQL_ROWID))
            or (after_rowid is not None
                and (upper_rowid is None or after_rowid > upper_rowid))
            or type(limit) is not int or not 1 <= limit <= MAX_REVIEW_LIFECYCLE_SOURCE_PAGE
            or type(max_source_bytes) is not int
            or not 1 <= max_source_bytes <= MAX_REVIEW_LIFECYCLE_PAGE_BYTES):
        raise ValueError("lifecycle source page requires a transaction and bounded cursor")


def _submission_at_rowid(
    db: sqlite3.Connection, repository: str, rowid: int, remaining_bytes: int,
) -> tuple[LegacyReviewSubmissionSource, int]:
    command_sizes = tuple(
        _size_expression(f"c.{column}")
        for column in LEGACY_SUBMISSION_COMMAND_COLUMNS
    )
    receipt_sizes = tuple(
        _size_expression(f"e.{column}")
        for column in LEGACY_SUBMISSION_RECEIPT_COLUMNS
    )
    sizes = (*command_sizes, *receipt_sizes)
    metadata = db.execute(
        f"SELECT max({','.join(sizes)}) AS largest_value,"
        f"{'+'.join(sizes)} AS total_bytes "
        "FROM lifecycle_commands c LEFT JOIN effect_receipts e "
        "ON e.command_id=c.command_id "
        "WHERE c.rowid=? AND c.repository=? AND c.kind='submit_review'",
        (rowid, repository),
    ).fetchone()
    if (metadata is None or type(metadata["largest_value"]) is not int
            or type(metadata["total_bytes"]) is not int):
        raise ValueError("review submission changed during its bounded read")
    if metadata["largest_value"] > MAX_LEGACY_SQL_TEXT_BYTES:
        raise ValueError("review submission source exceeds its field bound")
    if metadata["total_bytes"] > remaining_bytes:
        raise OverflowError("review submission exceeds the lifecycle page byte bound")
    command_fields = ",".join(
        f"typeof(c.{column}) AS command_type_{column},"
        f"CAST(c.{column} AS BLOB) AS command_raw_{column}"
        for column in LEGACY_SUBMISSION_COMMAND_COLUMNS
    )
    receipt_fields = ",".join(
        f"typeof(e.{column}) AS receipt_type_{column},"
        f"CAST(e.{column} AS BLOB) AS receipt_raw_{column}"
        for column in LEGACY_SUBMISSION_RECEIPT_COLUMNS
    )
    source = db.execute(
        f"SELECT {command_fields},{receipt_fields} "
        "FROM lifecycle_commands c LEFT JOIN effect_receipts e "
        "ON e.command_id=c.command_id "
        "WHERE c.rowid=? AND c.repository=? AND c.kind='submit_review'",
        (rowid, repository),
    ).fetchone()
    if source is None:
        raise ValueError("review submission changed during its bounded read")
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
    return LegacyReviewSubmissionSource(rowid, command, receipt), metadata["total_bytes"]


def _effect_at_rowid(
    db: sqlite3.Connection, repository: str, rowid: int,
    command_rowid: int, remaining_bytes: int,
) -> tuple[ReviewLifecycleEffectSource, int]:
    sizes = tuple(
        _size_expression(f"e.{column}")
        for column in LEGACY_SUBMISSION_RECEIPT_COLUMNS
    )
    metadata = db.execute(
        f"SELECT max({','.join(sizes)}) AS largest_value,"
        f"{'+'.join(sizes)} AS total_bytes "
        "FROM effect_receipts e WHERE e.rowid=?",
        (rowid,),
    ).fetchone()
    if (metadata is None or type(metadata["largest_value"]) is not int
            or type(metadata["total_bytes"]) is not int):
        raise ValueError("review effect changed during its bounded read")
    if metadata["largest_value"] > MAX_LEGACY_SQL_TEXT_BYTES:
        raise ValueError("review effect source exceeds its field bound")
    if metadata["total_bytes"] > remaining_bytes:
        raise OverflowError("review effect exceeds the lifecycle page byte bound")
    fields = ",".join(
        f"typeof({column}) AS type_{column},CAST({column} AS BLOB) AS raw_{column}"
        for column in LEGACY_SUBMISSION_RECEIPT_COLUMNS
    )
    row = db.execute(
        f"SELECT {fields} FROM effect_receipts WHERE rowid=?",
        (rowid,),
    ).fetchone()
    if row is None:
        raise ValueError("review effect changed during its bounded read")
    values = tuple(
        (column, _sql_text(row[f"type_{column}"], row[f"raw_{column}"]))
        for column in LEGACY_SUBMISSION_RECEIPT_COLUMNS
    )
    return ReviewLifecycleEffectSource(rowid, command_rowid, repository, values), metadata["total_bytes"]


def read_review_lifecycle_source_page(
    db: sqlite3.Connection, repository: str, *, after_rowid: int | None = None,
    upper_rowid: int | None = None, limit: int = MAX_REVIEW_LIFECYCLE_SOURCE_PAGE,
    max_source_bytes: int = MAX_REVIEW_LIFECYCLE_PAGE_BYTES,
) -> ObservedReviewLifecycleSourcePage | BlockedReviewLifecycleSourcePage:
    """Scan a fixed global command page, retaining matching review submissions.

    A future audit must also census effect rowids for orphaned or rekeyed
    receipts; this joined command page alone does not prove effect membership.
    """
    _validate_page_request(db, repository, after_rowid, upper_rowid, limit, max_source_bytes)
    version = read_review_lifecycle_source_version(db)
    if upper_rowid is None:
        edge = db.execute(
            "SELECT rowid FROM lifecycle_commands ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if edge is None:
            return ObservedReviewLifecycleSourcePage(
                repository, (), None, None, False, 0, 0, version,
            )
        upper_rowid = edge["rowid"]
    position = "" if after_rowid is None else "AND rowid>? "
    params: tuple[object, ...] = (*_selection_parameters(repository), upper_rowid)
    if after_rowid is not None:
        params = (*params, after_rowid)
    metadata = db.execute(
        "SELECT rowid AS source_rowid,"
        f"{_selection_columns('')} "
        "FROM lifecycle_commands "
        f"WHERE rowid<=? {position}ORDER BY rowid LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    sources: list[LegacyReviewSubmissionSource] = []
    source_bytes = 0
    scanned_rows = 0
    next_rowid = after_rowid
    for row in metadata[:limit]:
        rowid = row["source_rowid"]
        if _selected_review_source(row):
            if _selected_alias(row):
                return BlockedReviewLifecycleSourcePage(
                    "review submission repository or kind has a BLOB alias", rowid,
                )
            try:
                source, size = _submission_at_rowid(
                    db, repository, rowid, max_source_bytes - source_bytes,
                )
            except OverflowError:
                if scanned_rows:
                    break
                return BlockedReviewLifecycleSourcePage(
                    "review submission exceeds the lifecycle page byte bound", rowid,
                )
            except (TypeError, ValueError, UnicodeError, sqlite3.Error) as exc:
                return BlockedReviewLifecycleSourcePage(
                    f"review submission source is unreadable: {exc}"[:512], rowid,
                )
            sources.append(source)
            source_bytes += size
        scanned_rows += 1
        next_rowid = rowid
    return ObservedReviewLifecycleSourcePage(
        repository, tuple(sources), next_rowid, upper_rowid,
        len(metadata) > scanned_rows, scanned_rows, source_bytes, version,
    )


def read_review_lifecycle_effect_page(
    db: sqlite3.Connection, repository: str, *, after_rowid: int | None = None,
    upper_rowid: int | None = None, limit: int = MAX_REVIEW_LIFECYCLE_SOURCE_PAGE,
    max_source_bytes: int = MAX_REVIEW_LIFECYCLE_PAGE_BYTES,
) -> ObservedReviewLifecycleEffectPage | BlockedReviewLifecycleSourcePage:
    """Scan independent effect rowids and retain effects of selected commands."""
    _validate_page_request(db, repository, after_rowid, upper_rowid, limit, max_source_bytes)
    version = read_review_lifecycle_source_version(db)
    if upper_rowid is None:
        edge = db.execute(
            "SELECT rowid FROM effect_receipts ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if edge is None:
            return ObservedReviewLifecycleEffectPage(
                repository, (), None, None, False, 0, 0, version,
            )
        upper_rowid = edge["rowid"]
    position = "" if after_rowid is None else "AND e.rowid>? "
    params: tuple[object, ...] = (upper_rowid,)
    if after_rowid is not None:
        params = (*params, after_rowid)
    metadata = db.execute(
        "SELECT e.rowid AS source_rowid,typeof(e.command_id) AS command_id_type,"
        f"{_size_expression('e.command_id')} AS command_id_bytes "
        "FROM effect_receipts e "
        f"WHERE e.rowid<=? {position}ORDER BY e.rowid LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    sources: list[ReviewLifecycleEffectSource] = []
    source_bytes = 0
    scanned_rows = 0
    next_rowid = after_rowid
    for row in metadata[:limit]:
        rowid = row["source_rowid"]
        if row["command_id_type"] != "text":
            return BlockedReviewLifecycleSourcePage(
                "lifecycle effect has a non-text command identity", rowid,
            )
        if (type(row["command_id_bytes"]) is not int
                or row["command_id_bytes"] > MAX_LEGACY_SQL_TEXT_BYTES):
            return BlockedReviewLifecycleSourcePage(
                "lifecycle effect has an oversized command identity", rowid,
            )
        try:
            identity = db.execute(
                "SELECT CAST(command_id AS BLOB) AS raw_command_id "
                "FROM effect_receipts WHERE rowid=? "
                f"AND typeof(command_id)='text' AND {_size_expression('command_id')}<=?",
                (rowid, MAX_LEGACY_SQL_TEXT_BYTES),
            ).fetchone()
            if identity is None or not isinstance(identity["raw_command_id"], bytes):
                return BlockedReviewLifecycleSourcePage(
                    "lifecycle effect identity changed during its bounded read", rowid,
                )
            command_id = identity["raw_command_id"].decode("utf-8")
            parent = db.execute(
                "SELECT c.rowid AS command_rowid,"
                f"{_selection_columns('c.')} "
                "FROM lifecycle_commands c WHERE c.command_id=?",
                (*_selection_parameters(repository), command_id),
            ).fetchone()
        except (UnicodeError, sqlite3.Error) as exc:
            return BlockedReviewLifecycleSourcePage(
                f"lifecycle effect identity is unreadable: {exc}"[:512], rowid,
            )
        if parent is None:
            return BlockedReviewLifecycleSourcePage(
                "lifecycle effect has no matching command", rowid,
            )
        if _selected_review_source(parent):
            if _selected_alias(parent):
                return BlockedReviewLifecycleSourcePage(
                    "review effect command repository or kind has a BLOB alias", rowid,
                )
            try:
                source, size = _effect_at_rowid(
                    db, repository, rowid, parent["command_rowid"],
                    max_source_bytes - source_bytes,
                )
            except OverflowError:
                if scanned_rows:
                    break
                return BlockedReviewLifecycleSourcePage(
                    "review effect exceeds the lifecycle page byte bound", rowid,
                )
            except (TypeError, ValueError, UnicodeError, sqlite3.Error) as exc:
                return BlockedReviewLifecycleSourcePage(
                    f"review effect source is unreadable: {exc}"[:512], rowid,
                )
            sources.append(source)
            source_bytes += size
        scanned_rows += 1
        next_rowid = rowid
    return ObservedReviewLifecycleEffectPage(
        repository, tuple(sources), next_rowid, upper_rowid,
        len(metadata) > scanned_rows, scanned_rows, source_bytes, version,
    )
