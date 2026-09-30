"""Read-only inventory of legacy review execution files across state roots.

This inventory retains raw observations. Mapping issue counters to PRs and
validating their values require SQL run history and fresh linked-issue evidence.
"""

from __future__ import annotations

import os
import re
import stat
import sqlite3
import hashlib
import json
from dataclasses import (
    dataclass,
)

from ..domain.resolve_activation import LegacyAbsent, LegacyFile, LegacyPresent, LegacyUnreadable, UnmappedLegacyEntry
from ..domain.review_execution_activation import (
    MAX_LEGACY_SQL_TEXT_BYTES,
    MAX_SQL_ROWID,
    MIN_SQL_ROWID,
    LegacyExecutionCounterRecord,
    LegacyExecutionEntry,
    LegacyExecutionPRRecord,
    MAX_LEGACY_EXECUTION_FILE_ENTRIES,
    MAX_LEGACY_EXECUTION_FILE_BYTES,
    CompleteLegacyExecutionFileValues,
    BlockedLegacyExecutionFileValues,
    classify_legacy_execution_file_values,
    LegacyExecutionRunPage,
    LegacyExecutionRunRecord,
    LegacyReviewRunState,
    UnreadableLegacyExecutionRun,
    CompleteLegacyReviewRuns,
    BlockedLegacyReviewRuns,
    MAX_LEGACY_RUN_INVENTORY_ROWS,
    MAX_LEGACY_RUN_INVENTORY_BYTES,
    LEGACY_SUBMISSION_COMMAND_COLUMNS,
    LEGACY_SUBMISSION_RECEIPT_COLUMNS,
    LegacyReviewSubmissionPage,
    LegacyReviewSubmissionSource,
    UnreadableLegacyReviewSubmission,
    CompleteLegacyReviewSubmissions,
    BlockedLegacyReviewSubmissions,
    MAX_LEGACY_SUBMISSION_INVENTORY_ROWS,
    MAX_LEGACY_SUBMISSION_INVENTORY_BYTES,
    LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS,
    LEGACY_REVIEW_SUBMISSION_RECEIPT_NAMESPACES,
    LegacyReviewSubmissionReceiptPage,
    LegacyReviewSubmissionReceiptSource,
    UnreadableLegacyReviewSubmissionReceipt,
    CompleteLegacyReviewSubmissionReceipts,
    BlockedLegacyReviewSubmissionReceipts,
    MAX_LEGACY_SUBMISSION_RECEIPT_INVENTORY_ROWS,
    MAX_LEGACY_SUBMISSION_RECEIPT_INVENTORY_BYTES,
    MAX_LEGACY_REVIEW_OWNERS,
    LegacyReviewOwnerLink,
    LegacyReviewOwnerLineage,
    UnreadableLegacyReviewOwnerLineage,
    LEGACY_REVIEW_ROUND_COLUMNS,
    MAX_LEGACY_ROUND_JSON_BYTES,
    MAX_LEGACY_ROUND_PAGE_BYTES,
    LegacyReviewRoundPage,
    LegacyReviewRoundSource,
    UnreadableLegacyReviewRound,
    CompleteLegacyReviewRounds,
    BlockedLegacyReviewRounds,
    MAX_LEGACY_ROUND_INVENTORY_ROWS,
    MAX_LEGACY_ROUND_INVENTORY_BYTES,
    LEGACY_REVIEW_OUTCOME_RECEIPT_NAMESPACES,
    MAX_LEGACY_OUTCOME_RECEIPT_JSON_BYTES,
    LegacyReviewOutcomeReceiptPage,
    LegacyReviewOutcomeReceiptSource,
    UnreadableLegacyReviewOutcomeReceipt,
    BlockedLegacyReviewOutcomeReceiptPage,
    CompleteLegacyReviewOutcomeReceipts,
    BlockedLegacyReviewOutcomeReceipts,
    MAX_LEGACY_OUTCOME_RECEIPT_INVENTORY_ROWS,
    MAX_LEGACY_OUTCOME_RECEIPT_INVENTORY_BYTES,
    LegacyReviewGenerationReceiptSource,
    CompleteLegacyReviewGenerationReceipts,
    BlockedLegacyReviewGenerationReceipts,
    MAX_LEGACY_GENERATION_RECEIPT_ROWS,
    MAX_LEGACY_GENERATION_RECEIPT_BYTES,
    MAX_LEGACY_GENERATION_RECEIPT_JSON_BYTES,
)
from ..domain.review_execution import (
    NoReviewSession,
    ReviewRoundIdentity,
    ReviewExecutionBinding,
)
from ..domain.review_execution_session_import import (
    BlockedLegacyReviewSessionImport, UnprovenLegacyReviewSession, plan_legacy_review_session_import,
)
from ..domain.review_rounds import (
    ReviewRoundPlan,
)
from ..domain.workflow_identity import (
    PullRequestRef,
    canonical_workflow_repository,
    validate_workflow_identifier,
)
from .resolve_activation import LegacyRoots, _printable, _read_legacy_file, distinct_roots
from .workflow_codec import (
    decode_workflow,
    encode_workflow,
)
from .workflow_rows import (
    WorkflowAuthorityError,
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
MAX_LEGACY_EXECUTION_ROOTS = 64
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


@dataclass
class _LegacyExecutionFileBudget:
    names: int
    bytes: int


def read_legacy_execution_state(
    state_dir: str, root: str = "review", *, _budget: _LegacyExecutionFileBudget | None = None,
) -> tuple[LegacyExecutionEntry, ...]:
    """Every legacy review execution key in one labelled root, without writes."""
    budget = _budget if _budget is not None else _LegacyExecutionFileBudget(
        MAX_LEGACY_EXECUTION_FILE_ENTRIES, MAX_LEGACY_EXECUTION_FILE_BYTES,
    )
    try:
        names: list[str] = []
        with os.scandir(state_dir) as scan:
            for entry in scan:
                if budget.names == 0:
                    return (UnmappedLegacyEntry("<state-dir>", "legacy execution directory exceeds its entry bound", root),)
                budget.names -= 1
                names.append(entry.name)
        names.sort()
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
                source = _read_legacy_file(os.path.join(state_dir, name))
                if isinstance(source, LegacyAbsent):
                    source = LegacyUnreadable("listed review file disappeared during inventory")
                if isinstance(source, LegacyPresent):
                    budget.bytes -= len(source.text.encode("utf-8"))
                if budget.bytes < 0:
                    return (UnmappedLegacyEntry("<state-dir>", "legacy execution files exceed their byte bound", root),)
                pr_files.setdefault(pr, {})[suffix] = source
            continue
        counter_match = _COUNTER_NAME.fullmatch(name)
        if counter_match is not None:
            key = _number(counter_match.group(1), _SIGNED)
            if key is None:
                unmapped.append(UnmappedLegacyEntry(_printable(name), "not a canonical signed issue or PR key", root))
            else:
                source = _read_legacy_file(os.path.join(state_dir, name))
                if isinstance(source, LegacyAbsent):
                    source = LegacyUnreadable("listed review counter disappeared during inventory")
                if isinstance(source, LegacyPresent):
                    budget.bytes -= len(source.text.encode("utf-8"))
                if budget.bytes < 0:
                    return (UnmappedLegacyEntry("<state-dir>", "legacy execution files exceed their byte bound", root),)
                counters.append(LegacyExecutionCounterRecord(key, source, root))

    prs = [LegacyExecutionPRRecord(pr, found.get("session", LegacyAbsent()), found.get("round", LegacyAbsent()), root)
           for pr, found in sorted(pr_files.items())]
    return tuple(prs + sorted(counters, key=lambda record: record.key) + unmapped)


def read_legacy_execution_roots(roots: LegacyRoots) -> tuple[LegacyExecutionEntry, ...]:
    """Retain cross-root ambiguity while sharing one bounded scan budget."""
    if len(roots) > MAX_LEGACY_EXECUTION_ROOTS:
        return (UnmappedLegacyEntry("<state-dir>", "legacy execution root count exceeds its bound", "review"),)
    budget = _LegacyExecutionFileBudget(MAX_LEGACY_EXECUTION_FILE_ENTRIES, MAX_LEGACY_EXECUTION_FILE_BYTES)
    result: list[LegacyExecutionEntry] = []
    for label, path in distinct_roots(roots):
        entries = read_legacy_execution_state(path, label, _budget=budget)
        result.extend(entries)
        if any(isinstance(entry, UnmappedLegacyEntry) for entry in entries):
            break
    return tuple(result)


@dataclass(frozen=True)
class LegacyExecutionRootSource:
    label: str
    configured_path: str
    resolved_path: str
    directory_identity: tuple[int, int] | None

    def __post_init__(self) -> None:
        if (not all(isinstance(value, str) and value for value in (
                self.label, self.configured_path, self.resolved_path))
                or self.directory_identity is not None and (
                    not isinstance(self.directory_identity, tuple) or len(self.directory_identity) != 2
                    or any(type(value) is not int or value < 0 for value in self.directory_identity))):
            raise ValueError("legacy execution root source is invalid")


@dataclass(frozen=True)
class CompleteLegacyExecutionRootFiles:
    """Configured paths and their directory identities bind one complete file read."""

    roots: tuple[LegacyExecutionRootSource, ...]
    values: CompleteLegacyExecutionFileValues

    def __post_init__(self) -> None:
        if (not isinstance(self.roots, tuple) or len(self.roots) > MAX_LEGACY_EXECUTION_ROOTS
                or any(not isinstance(root, LegacyExecutionRootSource) for root in self.roots)
                or len({(root.label, root.configured_path) for root in self.roots}) != len(self.roots)
                or not isinstance(self.values, CompleteLegacyExecutionFileValues)):
            raise ValueError("complete legacy root files require bounded configured roots and parsed values")

    @property
    def source_digest(self) -> str:
        source = (
            tuple((root.label, root.configured_path, root.resolved_path, root.directory_identity)
                  for root in self.roots),
            self.values.source_digest,
        )
        return hashlib.sha256(json.dumps(source, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _legacy_execution_root_manifest(
    roots: LegacyRoots,
) -> tuple[LegacyExecutionRootSource, ...] | BlockedLegacyExecutionFileValues:
    if (not isinstance(roots, tuple) or len(roots) > MAX_LEGACY_EXECUTION_ROOTS
            or any(not isinstance(root, tuple) or len(root) != 2
                   or not isinstance(root[0], str) or not root[0]
                   or not isinstance(root[1], str) or not root[1] for root in roots)):
        return BlockedLegacyExecutionFileValues("legacy execution root configuration is invalid")
    sources: list[LegacyExecutionRootSource] = []
    for label, path in roots:
        try:
            configured_path = os.path.abspath(path)
            resolved_path = os.path.realpath(path)
            try:
                metadata = os.stat(path)
            except FileNotFoundError:
                identity = None
            else:
                if not stat.S_ISDIR(metadata.st_mode):
                    return BlockedLegacyExecutionFileValues("legacy execution root is not a directory")
                identity = (metadata.st_dev, metadata.st_ino)
            sources.append(LegacyExecutionRootSource(label, configured_path, resolved_path, identity))
        except (OSError, ValueError):
            return BlockedLegacyExecutionFileValues("legacy execution root cannot be resolved")
    if len({(root.label, root.configured_path) for root in sources}) != len(sources):
        return BlockedLegacyExecutionFileValues("legacy execution root configuration is duplicated")
    return tuple(sources)


def read_complete_legacy_execution_root_files(
    roots: LegacyRoots,
) -> CompleteLegacyExecutionRootFiles | BlockedLegacyExecutionFileValues:
    """Read each configured root with a stable path and directory identity."""
    before = _legacy_execution_root_manifest(roots)
    if isinstance(before, BlockedLegacyExecutionFileValues):
        return before
    try:
        values = classify_legacy_execution_file_values(read_legacy_execution_roots(roots))
    except (OSError, ValueError):
        return BlockedLegacyExecutionFileValues("legacy execution root read failed")
    if isinstance(values, BlockedLegacyExecutionFileValues):
        return values
    after = _legacy_execution_root_manifest(roots)
    if before != after:
        return BlockedLegacyExecutionFileValues("legacy execution roots changed during inventory")
    return CompleteLegacyExecutionRootFiles(before, values)


def revalidate_complete_legacy_execution_file_values(
    roots: LegacyRoots, observed: CompleteLegacyExecutionRootFiles,
) -> CompleteLegacyExecutionRootFiles | BlockedLegacyExecutionFileValues:
    """Re-read all configured roots under the caller's activation barriers."""
    if not isinstance(observed, CompleteLegacyExecutionRootFiles):
        return BlockedLegacyExecutionFileValues("legacy execution file revalidation requires a complete observation")
    current = read_complete_legacy_execution_root_files(roots)
    if isinstance(current, BlockedLegacyExecutionFileValues):
        return current
    if current.source_digest != observed.source_digest or current != observed:
        return BlockedLegacyExecutionFileValues("legacy execution roots or files changed or the supplied set was incomplete")
    return current


def select_legacy_review_execution_files(
    files: CompleteLegacyExecutionRootFiles, review_state_dir: str,
) -> CompleteLegacyExecutionFileValues | BlockedLegacyExecutionFileValues:
    """Select only the physical review root while retaining the full root audit."""
    if not isinstance(files, CompleteLegacyExecutionRootFiles):
        raise ValueError("review execution file selection requires complete roots")
    if not isinstance(review_state_dir, str) or not os.path.isabs(review_state_dir):
        raise ValueError("review execution file selection requires an absolute review directory")
    paths = {root.label: root.resolved_path for root in files.roots}
    review_path = os.path.realpath(review_state_dir)
    if len(paths) != len(files.roots) or review_path not in paths.values():
        return BlockedLegacyExecutionFileValues("configured review root is missing or its labels are ambiguous")
    labels = {label for label, path in paths.items() if path == review_path}
    return CompleteLegacyExecutionFileValues(
        tuple(item for item in files.values.prs if item.source.root in labels),
        tuple(item for item in files.values.counters if item.source.root in labels),
    )


def has_unattributed_historical_review_files(
    files: CompleteLegacyExecutionRootFiles, review_state_dir: str,
    *, other_role_state_dirs: tuple[str, ...], prs: frozenset[int], counter_keys: frozenset[int],
) -> bool:
    """Historical roots need producer provenance before active review state is imported."""
    current_paths = {os.path.realpath(path) for path in (review_state_dir, *other_role_state_dirs)}
    historical_labels = {
        root.label for root in files.roots
        if root.resolved_path not in current_paths
    }
    return (any(item.source.root in historical_labels and item.source.pr in prs
                for item in files.values.prs)
            or any(item.source.root in historical_labels and item.source.key in counter_keys
                   for item in files.values.counters))


def observe_legacy_review_session_import(
    roots: LegacyRoots,
    observed: CompleteLegacyExecutionRootFiles,
    binding: ReviewExecutionBinding,
    *, review_state_dir: str, other_role_state_dirs: tuple[str, ...] = (),
) -> NoReviewSession | UnprovenLegacyReviewSession | BlockedLegacyReviewSessionImport:
    """Re-read the configured file sources before proposing a session import.

    The caller still must hold activation barriers and bind this observation
    to the import decision and committed source audit.
    """
    if not isinstance(binding, ReviewExecutionBinding):
        raise ValueError("session import observation requires an execution binding")
    if (not isinstance(other_role_state_dirs, tuple)
            or any(not isinstance(path, str) or not os.path.isabs(path)
                   for path in other_role_state_dirs)):
        raise ValueError("session import observation requires absolute other-role paths")
    pr = binding.rejection.fence.key.target
    assert isinstance(pr, PullRequestRef)
    current = revalidate_complete_legacy_execution_file_values(roots, observed)
    if isinstance(current, BlockedLegacyExecutionFileValues):
        return BlockedLegacyReviewSessionImport(pr.number, current.reason)
    if has_unattributed_historical_review_files(
        current, review_state_dir, other_role_state_dirs=other_role_state_dirs,
        prs=frozenset((pr.number,)), counter_keys=frozenset(),
    ):
        return BlockedLegacyReviewSessionImport(pr.number, "historical review session or round source lacks root provenance")
    selected = select_legacy_review_execution_files(current, review_state_dir)
    if isinstance(selected, BlockedLegacyExecutionFileValues):
        return BlockedLegacyReviewSessionImport(pr.number, selected.reason)
    return plan_legacy_review_session_import(selected, current.source_digest, binding)


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


def read_checkpoint_round_row_source(
    db: sqlite3.Connection, round_id: ReviewRoundIdentity,
    *, max_source_bytes: int | None = None,
) -> LegacyReviewRoundSource:
    """Point-read one exact indexed round row without scanning retired history."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("checkpoint round source requires an explicit SQL transaction")
    if not isinstance(round_id, ReviewRoundIdentity):
        raise ValueError("checkpoint round source requires a typed round identity")
    if max_source_bytes is not None and (type(max_source_bytes) is not int or max_source_bytes < 1):
        raise ValueError("checkpoint round source requires a positive byte budget")
    try:
        aliases = db.execute(
            "SELECT rowid,typeof(round_id) AS round_id_type FROM review_execution_rounds "
            "WHERE round_id IN (?,CAST(? AS BLOB)) LIMIT 2",
            (round_id.round_id, round_id.round_id),
        ).fetchall()
        if len(aliases) != 1 or aliases[0]["round_id_type"] != "text":
            raise ValueError("checkpoint round has absent or aliased identity")
        json_columns = ("plan_json", "planning_json", "closure_json", "outcome_json")
        other_columns = tuple(column for column in LEGACY_REVIEW_ROUND_COLUMNS if column not in json_columns)
        json_sizes = [f"ifnull(length(CAST({column} AS BLOB)),0)" for column in json_columns]
        other_sizes = [f"ifnull(length(CAST({column} AS BLOB)),0)" for column in other_columns]
        metadata = db.execute(
            f"SELECT max({','.join(json_sizes)}) AS largest_json,"
            f"max({','.join(other_sizes)}) AS largest_other,"
            f"{'+'.join(json_sizes + other_sizes)} AS total_bytes "
            "FROM review_execution_rounds WHERE rowid=? AND round_id=?",
            (aliases[0]["rowid"], round_id.round_id),
        ).fetchone()
        if (metadata is None or metadata["largest_json"] is None
                or metadata["largest_json"] > MAX_LEGACY_ROUND_JSON_BYTES
                or metadata["largest_other"] > MAX_LEGACY_SQL_TEXT_BYTES
                or metadata["total_bytes"] > MAX_LEGACY_ROUND_PAGE_BYTES
                or (max_source_bytes is not None and metadata["total_bytes"] > max_source_bytes)):
            raise ValueError("checkpoint round row exceeds its bounded source")
        columns = ",".join(
            f"typeof({column}) AS type_{column},CAST({column} AS BLOB) AS raw_{column}"
            for column in LEGACY_REVIEW_ROUND_COLUMNS
        )
        row = db.execute(
            f"SELECT {columns} FROM review_execution_rounds WHERE rowid=? AND round_id=?",
            (aliases[0]["rowid"], round_id.round_id),
        ).fetchone()
        if row is None:
            raise ValueError("checkpoint round row changed during its bounded read")
        integer_columns = {"target_number", "execution_epoch", "generation", "sequence"}
        fields = tuple((column, (
            _sql_integer(row[f"type_{column}"], row[f"raw_{column}"])
            if column in integer_columns else _sql_text(row[f"type_{column}"], row[f"raw_{column}"])
        )) for column in LEGACY_REVIEW_ROUND_COLUMNS)
        source = LegacyReviewRoundSource(aliases[0]["rowid"], fields)
        values = dict(source.fields)
        plan = decode_workflow(values["plan_json"], ReviewRoundPlan)
        binding = round_id.binding
        if (values["repository"] != binding.rejection.fence.key.repository
                or values["target_number"] != binding.rejection.fence.key.target.number
                or values["execution_epoch"] != binding.execution_epoch
                or values["execution_episode"] != binding.execution_episode
                or values["generation"] != binding.rejection.fence.generation
                or values["sequence"] != round_id.sequence
                or plan.round != round_id
                or encode_workflow(plan) != values["plan_json"]):
            raise ValueError("checkpoint round row disagrees with its indexed plan")
        return source
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError,
            sqlite3.Error) as exc:
        raise WorkflowAuthorityError("review execution checkpoint round row is unreadable") from exc


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
