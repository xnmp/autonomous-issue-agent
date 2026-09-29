"""Schema-maintained mutation witnesses for paged review archive sources."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..domain.workflow_identity import canonical_workflow_repository
from .workflow_rows import WorkflowAuthorityError


MAX_ARCHIVE_SOURCE_VERSION = 9223372036854775806


@dataclass(frozen=True)
class ReviewArchiveSourceVersions:
    repository: str
    round_rows: int
    all_round_rows: int
    round_receipts: int
    generation_sources: int

    def __post_init__(self) -> None:
        if (canonical_workflow_repository(self.repository) != self.repository
                or type(self.round_rows) is not int
                or not 0 <= self.round_rows <= MAX_ARCHIVE_SOURCE_VERSION
                or type(self.all_round_rows) is not int
                or not 0 <= self.all_round_rows <= MAX_ARCHIVE_SOURCE_VERSION
                or type(self.round_receipts) is not int
                or not 0 <= self.round_receipts <= MAX_ARCHIVE_SOURCE_VERSION
                or type(self.generation_sources) is not int
                or not 0 <= self.generation_sources <= MAX_ARCHIVE_SOURCE_VERSION):
            raise ValueError("archive source versions require canonical bounded witnesses")


def _read_version(db: sqlite3.Connection, source: str, scope: bytes) -> int | None:
    row = db.execute(
        "SELECT typeof(source) AS source_type,typeof(scope) AS scope_type,"
        "typeof(version) AS version_type,version "
        "FROM review_archive_source_versions WHERE source=? AND scope=?",
        (source, scope),
    ).fetchone()
    if row is None:
        return None
    value = row["version"]
    if (row["source_type"] != "text" or row["scope_type"] != "blob"
            or row["version_type"] != "integer" or type(value) is not int
            or not 0 <= value <= MAX_ARCHIVE_SOURCE_VERSION):
        raise WorkflowAuthorityError("review archive source mutation witness is malformed")
    return value


def read_review_archive_source_versions(
    db: sqlite3.Connection, repository: str,
) -> ReviewArchiveSourceVersions:
    """Read all mutation witnesses in one explicit SQL snapshot.

    Equality across paged steps can show a stable source set once the audit
    cursors capture these values. This reader alone grants no authority.
    """
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or canonical_workflow_repository(repository) != repository):
        raise ValueError("archive source versions require a transaction and canonical repository")
    round_rows = _read_version(db, "round_rows", repository.encode())
    all_round_rows = _read_version(db, "all_round_rows", b"")
    round_receipts = _read_version(db, "round_receipts", b"")
    generation_sources = _read_version(db, "generation_sources", b"")
    if all_round_rows is None:
        raise WorkflowAuthorityError("global round row mutation witness is absent")
    if round_receipts is None:
        raise WorkflowAuthorityError("round receipt mutation witness is absent")
    if generation_sources is None:
        raise WorkflowAuthorityError("generation source mutation witness is absent")
    if round_rows is None:
        existing = db.execute(
            "SELECT 1 FROM review_execution_rounds INDEXED BY review_execution_rounds_repository "
            "WHERE repository IN (?,CAST(? AS BLOB)) LIMIT 1",
            (repository, repository),
        ).fetchone()
        workflow = db.execute(
            "SELECT 1 FROM workflows WHERE repository IN (?,CAST(? AS BLOB)) LIMIT 1",
            (repository, repository),
        ).fetchone()
        if existing is not None or workflow is not None:
            raise WorkflowAuthorityError("round row mutation witness is absent")
        round_rows = 0
    return ReviewArchiveSourceVersions(
        repository, round_rows, all_round_rows, round_receipts, generation_sources,
    )
