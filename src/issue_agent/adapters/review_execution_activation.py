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
)
from .resolve_activation import LegacyRoots, _printable, _read_legacy_file, distinct_roots

_PR_NAME = re.compile(r"pr-(.*)\.(session|round)", re.DOTALL)
_COUNTER_NAME = re.compile(r"issue-(.*)\.infra-deaths", re.DOTALL)
_POSITIVE = re.compile(r"[1-9][0-9]{0,18}")
_SIGNED = re.compile(r"-?[1-9][0-9]{0,18}")
_MAX_NUMBER = 2**63 - 1
MAX_RUN_SOURCE_PAGE = 256
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
