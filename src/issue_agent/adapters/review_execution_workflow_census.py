"""Bounded transaction-local census of retained review-rejection workflows."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3

from ..domain.review_activation import OpenPullRequestsListed
from ..domain.review_execution import ExecutionNotActivated
from ..domain.review_workflow import ReviewWorkflowSnapshot
from ..domain.workflow_identity import PullRequestRef, WorkflowKey, canonical_workflow_repository
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, encode_workflow
from .workflow_rows import WorkflowAuthorityError, load_review_workflow_row

MAX_RETAINED_REVIEW_ROWS = 4096
MAX_RETAINED_REVIEW_BYTES = 8 * 1024 * 1024
MAX_WORKFLOW_CENSUS_SCANNED_ROWS = 65536


@dataclass(frozen=True)
class RetainedReviewWorkflowSource:
    rowid: int
    snapshot: ReviewWorkflowSnapshot
    row_digest: str

    def __post_init__(self) -> None:
        if (type(self.rowid) is not int or not -(2**63) <= self.rowid <= 2**63 - 1
                or not isinstance(self.snapshot, ReviewWorkflowSnapshot)
                or not isinstance(self.snapshot.execution, ExecutionNotActivated)
                or not isinstance(self.row_digest, str) or len(self.row_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.row_digest)):
            raise ValueError("retained review source requires an inactive canonical workflow row")


@dataclass(frozen=True)
class CompleteRetainedReviewWorkflows:
    repository: str
    listing: OpenPullRequestsListed
    sources: tuple[RetainedReviewWorkflowSource, ...]
    source_digest: str

    def __post_init__(self) -> None:
        if (canonical_workflow_repository(self.repository) != self.repository
                or not isinstance(self.listing, OpenPullRequestsListed)
                or not isinstance(self.sources, tuple)
                or len(self.sources) > MAX_RETAINED_REVIEW_ROWS
                or any(not isinstance(item, RetainedReviewWorkflowSource) for item in self.sources)
                or (numbers := tuple(item.snapshot.revision.fence.key.target.number
                                     for item in self.sources)) != tuple(sorted(set(numbers)))
                or not set(self.listing.numbers) <= set(numbers)
                or not isinstance(self.source_digest, str) or len(self.source_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.source_digest)):
            raise ValueError("retained review census requires all listed PR workflows")


@dataclass(frozen=True)
class BlockedRetainedReviewWorkflows:
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason or len(self.reason) > 512:
            raise ValueError("retained review census blocker must be bounded text")


def read_complete_retained_review_workflows(
    db: sqlite3.Connection, repository: str, listing: OpenPullRequestsListed,
    *, max_rows: int = MAX_RETAINED_REVIEW_ROWS, max_bytes: int = MAX_RETAINED_REVIEW_BYTES,
) -> CompleteRetainedReviewWorkflows | BlockedRetainedReviewWorkflows:
    """Compare every retained PR row to one complete listing in the caller's snapshot.

    Canonical decoding delegates to the normal hot reader. The caller still
    needs fresh remote revalidation, effect barriers and all other import
    source inventories before using this observation in an activation.
    """
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or canonical_workflow_repository(repository) != repository
            or not isinstance(listing, OpenPullRequestsListed)
            or type(max_rows) is not int or not 1 <= max_rows <= MAX_RETAINED_REVIEW_ROWS
            or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RETAINED_REVIEW_BYTES):
        raise ValueError("retained review census requires a transaction and bounded inputs")
    # The exact repository/kind index cannot find case, whitespace or BLOB
    # aliases. A bounded rowid pass accounts for those before the indexed PR
    # payload reads; scan exhaustion refuses rather than claiming completeness.
    try:
        global_rows = db.execute(
            "SELECT rowid,typeof(repository) AS repository_type,"
            "typeof(target_kind) AS target_type,"
            "length(CAST(repository AS BLOB)) AS repository_bytes,"
            "length(CAST(target_kind AS BLOB)) AS target_bytes,"
            "CASE WHEN length(CAST(repository AS BLOB))<=256 "
            "THEN CAST(repository AS TEXT) END AS repository_value,"
            "CASE WHEN length(CAST(target_kind AS BLOB))<=64 "
            "THEN CAST(target_kind AS TEXT) END AS target_value "
            "FROM workflows ORDER BY rowid LIMIT ?",
            (MAX_WORKFLOW_CENSUS_SCANNED_ROWS + 1,),
        ).fetchall()
    except (sqlite3.Error, UnicodeError):
        return BlockedRetainedReviewWorkflows("retained workflow scope scan is unreadable")
    if len(global_rows) > MAX_WORKFLOW_CENSUS_SCANNED_ROWS:
        return BlockedRetainedReviewWorkflows("retained workflow scope scan exceeds its row bound")
    rowids: list[int] = []
    for row in global_rows:
        repo, kind = row["repository_value"], row["target_value"]
        if (type(row["repository_bytes"]) is not int or row["repository_bytes"] > 256
                or type(row["target_bytes"]) is not int or row["target_bytes"] > 64
                or not isinstance(repo, str) or not isinstance(kind, str)):
            return BlockedRetainedReviewWorkflows("retained workflow has malformed scope metadata")
        if repo.strip().casefold() != repository:
            continue
        if row["repository_type"] != "text" or repo != repository:
            return BlockedRetainedReviewWorkflows("retained workflow uses a repository alias")
        if row["target_type"] != "text" or kind not in {"issue", "pr"}:
            return BlockedRetainedReviewWorkflows("retained workflow has a malformed target kind")
        if kind == "pr":
            rowids.append(row["rowid"])
            if len(rowids) > max_rows:
                return BlockedRetainedReviewWorkflows("retained review row count exceeds the census bound")
    if len(listing.numbers) > max_rows:
        return BlockedRetainedReviewWorkflows("retained review row count exceeds the census bound")
    rows = [db.execute(
        "SELECT rowid,typeof(repository) AS repository_type,"
        "typeof(target_kind) AS target_type,typeof(target_number) AS number_type,"
        "typeof(generation) AS generation_type,typeof(version) AS version_type,"
        "typeof(episode_json) AS payload_type,"
        "length(CAST(repository AS BLOB)) AS repository_bytes,"
        "length(CAST(target_kind AS BLOB)) AS target_bytes,"
        "length(CAST(target_number AS BLOB)) AS number_bytes,"
        "length(CAST(generation AS BLOB)) AS generation_bytes,"
        "length(CAST(version AS BLOB)) AS version_bytes,"
        "length(CAST(episode_json AS BLOB)) AS payload_bytes "
        "FROM workflows WHERE rowid=?", (rowid,),
    ).fetchone() for rowid in rowids]
    if any(row is None for row in rows):
        return BlockedRetainedReviewWorkflows("retained review row changed during census")
    budget = 0
    for row in rows:
        sizes = tuple(row[name] for name in (
            "repository_bytes", "target_bytes", "number_bytes", "generation_bytes",
            "version_bytes", "payload_bytes",
        ))
        if (tuple(row[name] for name in (
            "repository_type", "target_type", "number_type", "generation_type",
            "version_type", "payload_type",
        )) != ("text", "text", "integer", "integer", "integer", "text")
                or any(type(size) is not int or size < 0 for size in sizes)
                or sizes[-1] > MAX_WORKFLOW_PAYLOAD_BYTES):
            return BlockedRetainedReviewWorkflows("retained review row has malformed types or oversized payload")
        budget += sum(sizes)
        if budget > max_bytes:
            return BlockedRetainedReviewWorkflows("retained review rows exceed the census byte bound")
    sources: list[RetainedReviewWorkflowSource] = []
    for metadata in rows:
        row = db.execute(
            "SELECT repository,target_kind,target_number,generation,version,episode_json "
            "FROM workflows WHERE rowid=?", (metadata["rowid"],),
        ).fetchone()
        if row is None or row["repository"] != repository or row["target_kind"] != "pr":
            return BlockedRetainedReviewWorkflows("retained review row changed or uses a repository alias")
        number = row["target_number"]
        if type(number) is not int or not 0 < number <= 2**63 - 1:
            return BlockedRetainedReviewWorkflows("retained review row has an invalid PR number")
        try:
            key = WorkflowKey(repository, PullRequestRef(number))
            snapshot = load_review_workflow_row(db, key)
            if snapshot is None or not isinstance(snapshot.execution, ExecutionNotActivated):
                return BlockedRetainedReviewWorkflows("retained review row has active or missing execution")
            encoded = encode_workflow(snapshot)
        except (WorkflowAuthorityError, ValueError, TypeError) as exc:
            return BlockedRetainedReviewWorkflows(f"retained review PR #{number} is not canonical: {str(exc)[:400]}")
        row_digest = hashlib.sha256(json.dumps((
            metadata["rowid"], row["repository"], row["target_kind"], number,
            row["generation"], row["version"], row["episode_json"], encoded,
        ), separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        sources.append(RetainedReviewWorkflowSource(metadata["rowid"], snapshot, row_digest))
    sources.sort(key=lambda item: item.snapshot.revision.fence.key.target.number)
    numbers = tuple(item.snapshot.revision.fence.key.target.number for item in sources)
    if not set(listing.numbers) <= set(numbers):
        missing = tuple(sorted(set(listing.numbers) - set(numbers)))
        return BlockedRetainedReviewWorkflows(
            f"retained review membership differs: missing={missing[:8]}"
        )
    digest = hashlib.sha256(json.dumps((
        repository, listing.numbers, tuple((item.rowid, item.row_digest) for item in sources),
    ), separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    return CompleteRetainedReviewWorkflows(repository, listing, tuple(sources), digest)
