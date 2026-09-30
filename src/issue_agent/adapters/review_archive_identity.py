"""Paged global identity projection for archived review source lookups."""

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import sqlite3

from ..domain.review_archive_identity import (
    ARCHIVE_IDENTITY_TABLES, BlockedReviewArchiveIdentities, CompletedReviewArchiveIdentities,
    ReviewArchiveIdentityCursor, ReviewArchiveIdentityState, ScanningReviewArchiveIdentities,
)
from .review_archive_source_versions import _read_version, read_review_lifecycle_source_version
from .sql_read_budget import bounded_sql_source_reads
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import WorkflowAuthorityError


_NAMESPACE = "review-archive-identity-scan-v1"
_SCOPE = "global"
_KEY = "identities"
_PAGE_SIZE = 128
_PAGE_BYTES = 1024 * 1024
_MATCH_LIMIT = 128
_MEMBER_PAYLOAD_BYTES = 8192
_SOURCES = {
    "dedupe_keys": (("namespace", "scope", "key"), (256, 256, 512)),
    "review_execution_rounds": (("repository", "target_kind", "round_id"), (256, 64, 512)),
    "dispatch_claims": (("repository", "role", "occurrence_id"), (256, 64, 512)),
}


def _versions(db: sqlite3.Connection) -> tuple[int, int]:
    rounds = _read_version(db, "all_round_rows", b"")
    if rounds is None:
        raise WorkflowAuthorityError("global round source witness is absent")
    return rounds, read_review_lifecycle_source_version(db)


def _snapshot(versions: tuple[int, int], projection_epoch: int) -> str:
    return hashlib.sha256(encode_workflow((versions, projection_epoch)).encode()).hexdigest()


def _projection_version(db: sqlite3.Connection) -> int:
    version = _read_version(db, "identity_members", b"")
    if version is None:
        raise WorkflowAuthorityError("archive identity projection witness is absent")
    return version


def _state(db: sqlite3.Connection) -> tuple[int | None, ReviewArchiveIdentityState | None, int | None]:
    rows = db.execute(
        "SELECT rowid,typeof(namespace),typeof(scope),typeof(key),typeof(payload_json),"
        "octet_length(payload_json) FROM level_state WHERE namespace IN (?,CAST(? AS BLOB)) "
        "AND scope IN (?,CAST(? AS BLOB)) AND key IN (?,CAST(? AS BLOB)) LIMIT 2",
        (_NAMESPACE, _NAMESPACE, _SCOPE, _SCOPE, _KEY, _KEY),
    ).fetchall()
    if not rows:
        return None, None, None
    row = tuple(rows[0])
    if (len(rows) != 1 or row[1:5] != ("text",) * 4
            or type(row[5]) is not int or row[5] > MAX_WORKFLOW_PAYLOAD_BYTES):
        raise WorkflowAuthorityError("archive identity cursor is aliased or oversized")
    raw = db.execute("SELECT payload_json FROM level_state WHERE rowid=?", (row[0],)).fetchone()
    try:
        value = decode_workflow(raw[0], ReviewArchiveIdentityCursor)
        if encode_workflow(value) != raw[0]:
            raise ValueError("noncanonical cursor")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowAuthorityError("archive identity cursor is malformed") from exc
    return row[0], value.state, value.projection_version


def _has_state_receipt(
    db: sqlite3.Connection, state: ReviewArchiveIdentityState, projection_version: int,
) -> bool:
    versions = (state.prior.source_versions if isinstance(state, BlockedReviewArchiveIdentities)
                else state.source_versions)
    metadata = db.execute(
        "SELECT typeof(round_rows_version),typeof(lifecycle_version),"
        "typeof(projection_version),typeof(state_json),octet_length(state_json) "
        "FROM review_archive_identity_certifications WHERE round_rows_version=? "
        "AND lifecycle_version=? AND projection_version=?",
        (*versions, projection_version),
    ).fetchone()
    if metadata is None or tuple(metadata)[:4] != ("integer", "integer", "integer", "text"):
        return False
    size = metadata[4]
    if type(size) is not int or size > MAX_WORKFLOW_PAYLOAD_BYTES:
        return False
    row = db.execute(
        "SELECT state_json FROM review_archive_identity_certifications "
        "WHERE round_rows_version=? AND lifecycle_version=? AND projection_version=?",
        (*versions, projection_version),
    ).fetchone()
    if row is None:
        return False
    try:
        recorded = decode_workflow(row[0], ReviewArchiveIdentityState)
        return recorded == state and encode_workflow(recorded) == row[0]
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return False


def _save(db: sqlite3.Connection, rowid: int | None, value: ReviewArchiveIdentityState, now: datetime) -> None:
    prior = _projection_version(db)
    predicted_post = prior + 1
    versions = (value.prior.source_versions if isinstance(value, BlockedReviewArchiveIdentities)
                else value.source_versions)
    db.execute(
        "INSERT INTO review_archive_identity_certifications"
        "(round_rows_version,lifecycle_version,projection_version,state_json) VALUES (?,?,?,?)",
        (*versions, predicted_post, encode_workflow(value)),
    )
    if _projection_version(db) != predicted_post:
        raise WorkflowAuthorityError("archive identity state witness changed")
    payload = encode_workflow(ReviewArchiveIdentityCursor(value, _projection_version(db)))
    if rowid is None:
        db.execute(
            "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) VALUES (?,?,?,?,?)",
            (_NAMESPACE, _SCOPE, _KEY, payload, now.isoformat()),
        )
    elif db.execute(
        "UPDATE level_state SET payload_json=?,updated_at=? WHERE rowid=? "
        "AND namespace=? AND scope=? AND key=?",
        (payload, now.isoformat(), rowid, _NAMESPACE, _SCOPE, _KEY),
    ).rowcount != 1:
        raise WorkflowAuthorityError("archive identity cursor changed during update")


def _begin(db: sqlite3.Connection, versions: tuple[int, int], now: datetime,
           due_seconds: int) -> ScanningReviewArchiveIdentities:
    table = ARCHIVE_IDENTITY_TABLES[0]
    upper = db.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0]
    return ScanningReviewArchiveIdentities(
        versions, _projection_version(db), table, None, upper, (0, 0, 0),
        now, now + timedelta(seconds=due_seconds),
    )


def _next_table(db: sqlite3.Connection, scan: ScanningReviewArchiveIdentities,
                now: datetime, due_seconds: int) -> ReviewArchiveIdentityState:
    index = ARCHIVE_IDENTITY_TABLES.index(scan.table) + 1
    if index == len(ARCHIVE_IDENTITY_TABLES):
        return CompletedReviewArchiveIdentities(
            scan.source_versions, scan.projection_epoch, scan.counts,
            now, now + timedelta(seconds=due_seconds),
        )
    table = ARCHIVE_IDENTITY_TABLES[index]
    upper = db.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0]
    return replace(scan, table=table, after_rowid=None, upper_rowid=upper)


def _page(db: sqlite3.Connection, scan: ScanningReviewArchiveIdentities,
          now: datetime, due_seconds: int) -> ReviewArchiveIdentityState:
    table = scan.table
    if scan.upper_rowid is None:
        return _next_table(db, scan, now, due_seconds)
    columns, limits = _SOURCES[table]
    lower = "" if scan.after_rowid is None else "AND rowid>?"
    fields = ",".join(
        ("rowid", *(f"typeof({name})" for name in columns),
         *(f"octet_length({name})" for name in columns))
    )
    parameters = ((scan.after_rowid,) if scan.after_rowid is not None else ())
    metadata = db.execute(
        f"SELECT {fields} FROM {table} WHERE rowid<=? {lower} ORDER BY rowid LIMIT ?",
        (scan.upper_rowid, *parameters, _PAGE_SIZE + 1),
    ).fetchall()
    selected = metadata[:_PAGE_SIZE]
    budget = 0
    digest = _snapshot(scan.source_versions, scan.projection_epoch)
    for item in selected:
        row = tuple(item)
        rowid, types, sizes = row[0], row[1:4], row[4:7]
        if (type(rowid) is not int or any(kind not in {"text", "blob"} for kind in types)
                or any(type(size) is not int or size > limit
                       for size, limit in zip(sizes, limits, strict=True))):
            raise WorkflowAuthorityError(f"archive identity {table} has malformed or oversized identity")
        budget += sum(sizes)
        if budget > _PAGE_BYTES:
            raise WorkflowAuthorityError("archive identity page exceeds its byte bound")
        values = db.execute(
            f"SELECT {','.join(f'CAST({name} AS TEXT)' for name in columns)} "
            f"FROM {table} WHERE rowid=?", (rowid,),
        ).fetchone()
        if values is None or any(not isinstance(value, str) for value in values):
            raise WorkflowAuthorityError(f"archive identity {table} changed during read")
        raw = tuple(values)
        normalized = tuple(value.strip().casefold() for value in raw)
        db.execute(
            "INSERT INTO review_archive_identity_members"
            "(source_table,source_rowid,snapshot_sha256,first,second,third,types_json,values_json) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_table,source_rowid) DO UPDATE SET "
            "snapshot_sha256=excluded.snapshot_sha256,first=excluded.first,"
            "second=excluded.second,third=excluded.third,types_json=excluded.types_json,"
            "values_json=excluded.values_json",
            (table, rowid, digest, *normalized, encode_workflow(tuple(types)),
             encode_workflow(raw)),
        )
    count = len(selected)
    counts = list(scan.counts)
    counts[ARCHIVE_IDENTITY_TABLES.index(table)] += count
    updated = replace(scan, after_rowid=selected[-1][0] if selected else scan.after_rowid,
                      counts=tuple(counts))
    return updated if len(metadata) > _PAGE_SIZE else _next_table(db, updated, now, due_seconds)


def advance_review_archive_identity_audit(
    db: sqlite3.Connection, *, now: datetime, due_seconds: int = 3600,
) -> ReviewArchiveIdentityState:
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or type(due_seconds) is not int or not 1 <= due_seconds <= 86400):
        raise ValueError("archive identity audit requires a transaction and bounded aware time")
    with bounded_sql_source_reads(db, _PAGE_BYTES):
        versions = _versions(db)
        rowid, state, stored_projection_version = _state(db)
        projection_changed = (stored_projection_version is not None
                              and stored_projection_version != _projection_version(db))
        uncertified = (state is not None and stored_projection_version is not None
                       and not _has_state_receipt(db, state, stored_projection_version))
        if isinstance(state, BlockedReviewArchiveIdentities):
            if now < state.detected_at:
                raise ValueError("archive identity audit time moved backwards")
            if state.prior.source_versions == versions and not projection_changed and not uncertified:
                return state
            state = state.prior
        if isinstance(state, ScanningReviewArchiveIdentities) and now < state.started_at:
            raise ValueError("archive identity audit time moved backwards")
        if isinstance(state, CompletedReviewArchiveIdentities) and now < state.completed_at:
            raise ValueError("archive identity audit time moved backwards")
        if (state is None or state.source_versions != versions or projection_changed or uncertified
                or isinstance(state, CompletedReviewArchiveIdentities)
                and now >= state.next_due_at):
            state = _begin(db, versions, now, due_seconds)
        elif isinstance(state, CompletedReviewArchiveIdentities):
            return state
        try:
            result = _page(db, state, now, due_seconds)
        except (WorkflowAuthorityError, UnicodeError, sqlite3.Error) as exc:
            result = BlockedReviewArchiveIdentities(state, (str(exc) or "malformed identity")[:512], now)
        _save(db, rowid, result, now)
        return result


def require_review_archive_identity_audit(
    db: sqlite3.Connection, *, now: datetime | None = None,
) -> CompletedReviewArchiveIdentities:
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or now is not None and (not isinstance(now, datetime) or now.tzinfo is None
                                    or now.utcoffset() is None)):
        raise ValueError("archive identity certification requires a transaction and aware time")
    with bounded_sql_source_reads(db, _PAGE_BYTES):
        _, state, stored_projection_version = _state(db)
        if (not isinstance(state, CompletedReviewArchiveIdentities)
                or state.source_versions != _versions(db)
                or stored_projection_version != _projection_version(db)
                or not _has_state_receipt(db, state, stored_projection_version)
                or now is not None and not state.completed_at <= now < state.next_due_at):
            raise WorkflowAuthorityError("archive identity projection is incomplete or stale")
        return state


def _prefix_upper(prefix: str) -> str | None:
    for position in range(len(prefix) - 1, -1, -1):
        if ord(prefix[position]) < 0x10ffff:
            return prefix[:position] + chr(ord(prefix[position]) + 1)
    return None


def matching_review_archive_identity_rowids(
    db: sqlite3.Connection, table: str, expected: tuple[str, str, str], *,
    prefix_last: bool = False,
) -> list[int]:
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(table, str) or table not in _SOURCES
            or not isinstance(expected, tuple)
            or len(expected) != 3 or any(not isinstance(value, str) for value in expected)
            or type(prefix_last) is not bool):
        raise ValueError("archive identity lookup requires a selected table and three text values")
    with bounded_sql_source_reads(db, _PAGE_BYTES):
        state = require_review_archive_identity_audit(db)
        digest = _snapshot(state.source_versions, state.projection_epoch)
        normalized = tuple(value.strip().casefold() for value in expected)
        if prefix_last:
            upper = _prefix_upper(normalized[2])
            condition = "AND third>=?" + (" AND third<?" if upper is not None else "")
            parameters = (*normalized[:2], normalized[2], *((upper,) if upper else ()))
        else:
            condition = "AND third=?"
            parameters = normalized
        rows = db.execute(
            "SELECT source_rowid,typeof(types_json),octet_length(types_json),"
            "typeof(values_json),octet_length(values_json) FROM review_archive_identity_members "
            "INDEXED BY review_archive_identity_lookup WHERE source_table=? AND snapshot_sha256=? "
            f"AND first=? AND second=? {condition} ORDER BY third,source_rowid LIMIT ?",
            (table, digest, *parameters, _MATCH_LIMIT + 1),
        ).fetchall()
        if len(rows) > _MATCH_LIMIT:
            raise WorkflowAuthorityError("archive identity lookup exceeds match bound")
        matched = []
        for metadata in rows:
            rowid, types_kind, types_size, values_kind, values_size = tuple(metadata)
            if (type(rowid) is not int or types_kind != "text" or values_kind != "text"
                    or type(types_size) is not int or type(values_size) is not int
                    or types_size > _MEMBER_PAYLOAD_BYTES
                    or values_size > _MEMBER_PAYLOAD_BYTES):
                raise WorkflowAuthorityError("archive identity member is oversized or malformed")
            row = db.execute(
                "SELECT types_json,values_json FROM review_archive_identity_members "
                "WHERE source_table=? AND source_rowid=? AND snapshot_sha256=?",
                (table, rowid, digest),
            ).fetchone()
            if row is None:
                raise WorkflowAuthorityError("archive identity member changed during read")
            try:
                types = decode_workflow(row[0], tuple)
                values = decode_workflow(row[1], tuple)
                if (encode_workflow(types) != row[0] or encode_workflow(values) != row[1]
                        or len(types) != 3 or len(values) != 3):
                    raise ValueError("noncanonical projected identity")
            except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                raise WorkflowAuthorityError("archive identity member is malformed") from exc
            if (types != ("text",) * 3 or values[:2] != expected[:2]
                    or not isinstance(values[2], str)
                    or (not prefix_last and values[2] != expected[2])
                    or (prefix_last and not values[2].startswith(expected[2]))):
                raise WorkflowAuthorityError(f"archive claim {table} identity is aliased")
            matched.append(rowid)
        return sorted(matched)
