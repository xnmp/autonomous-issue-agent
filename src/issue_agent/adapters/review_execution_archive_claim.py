"""Bounded historical SQL proof of one review round's parent claim."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import sqlite3
from typing import Literal

from ..domain.review_execution import ReviewRoundIdentity
from ..domain.review_preflight import ReviewPreflightSelection
from ..domain.review_rounds import MAX_REVIEW_STAGES, ReviewRoundPlan
from ..domain.workflow_identity import PullRequestRef
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_STAGE_NAMESPACE, WorkflowAuthorityError, require_review_stage_receipts,
    review_stage_receipt_prefix,
)

MAX_ARCHIVE_CLAIM_SOURCE_BYTES = 4 * 1024 * 1024
_MAX_GLOBAL_ROWS = 65_536
_SQL_LENGTH_LIMIT = 8 * MAX_WORKFLOW_PAYLOAD_BYTES
_LIFECYCLE = "dispatch-claim-lifecycle"
_PREFLIGHT = "review-round-preflight"


@dataclass(frozen=True)
class ObservedReviewExecutionArchiveClaim:
    owner_id: str
    occurrence_id: str
    terminal_kind: Literal["live", "released", "reclaimed"]
    source_sha256: str
    source_bytes: int

    def __post_init__(self) -> None:
        if (not self.owner_id or not self.occurrence_id
                or self.terminal_kind not in ("live", "released", "reclaimed")
                or len(self.source_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.source_sha256)
                or type(self.source_bytes) is not int or self.source_bytes <= 0):
            raise ValueError("archive claim observation requires bounded provenance")


def _octets(column: str) -> str:
    return (f"ifnull(octet_length({column}),0)" if sqlite3.sqlite_version_info >= (3, 43, 0)
            else f"ifnull(length(CAST({column} AS BLOB)),0)")


def _global_identities(
    db: sqlite3.Connection, table: str, columns: tuple[str, ...], limits: tuple[int, ...],
) -> Iterator[tuple[object, ...]]:
    sizes = tuple(_octets(column) for column in columns)
    projection = ",".join((
        "rowid", *(f"typeof({column})" for column in columns), *sizes,
        *(f"CASE WHEN {size}<=? THEN CAST({column} AS TEXT) END"
          for column, size in zip(columns, sizes, strict=True)),
    ))
    rows = db.execute(
        f"SELECT {projection} FROM {table} ORDER BY rowid LIMIT ?",
        (*limits, _MAX_GLOBAL_ROWS + 1),
    )
    width = len(columns)
    for count, row in enumerate(rows, start=1):
        if count > _MAX_GLOBAL_ROWS:
            raise WorkflowAuthorityError(f"archive claim {table} identity census exceeds row bound")
        item = tuple(row)
        types, lengths, values = item[1:1 + width], item[1 + width:1 + 2 * width], item[1 + 2 * width:]
        if any(type(size) is not int or size > limit or not isinstance(value, str)
               for size, limit, value in zip(lengths, limits, values, strict=True)):
            raise WorkflowAuthorityError(f"archive claim {table} identity is malformed or oversized")
        yield (item[0], *types, *values)


def _matching_rowids(
    db: sqlite3.Connection, table: str, columns: tuple[str, ...], limits: tuple[int, ...],
    expected: tuple[str, ...], *, prefix_last: bool = False, indexed_identities: bool = False,
) -> list[int]:
    if indexed_identities:
        from .review_archive_identity import matching_review_archive_identity_rowids

        return matching_review_archive_identity_rowids(
            db, table, (expected[0], expected[1], expected[2]), prefix_last=prefix_last,
        )
    rows = _global_identities(db, table, columns, limits)
    matched = []
    for row in rows:
        width = len(columns)
        rowid, types, values = row[0], row[1:1 + width], row[1 + width:]
        normalized = tuple(value.strip().casefold() for value in values)
        required = tuple(value.casefold() for value in expected)
        matches = (normalized[:-1] == required[:-1]
                   and normalized[-1].startswith(required[-1]) if prefix_last
                   else normalized == required)
        if not matches:
            continue
        if any(kind != "text" for kind in types) or (not prefix_last and values != expected):
            raise WorkflowAuthorityError(f"archive claim {table} identity is aliased")
        if prefix_last and (values[:-1] != expected[:-1] or not values[-1].startswith(expected[-1])):
            raise WorkflowAuthorityError(f"archive claim {table} identity is aliased")
        matched.append(rowid)
    return matched


def _bounded_row(
    db: sqlite3.Connection, table: str, rowid: int, columns: tuple[str, ...],
    *, nullable: frozenset[str] = frozenset(), json_columns: frozenset[str] = frozenset(),
) -> tuple[tuple[object, ...], int]:
    sizes = tuple(_octets(column) for column in columns)
    metadata = db.execute(
        f"SELECT {','.join(f'typeof({column})' for column in columns)},"
        f"{','.join(sizes)} FROM {table} WHERE rowid=?", (rowid,),
    ).fetchone()
    if metadata is None:
        raise WorkflowAuthorityError(f"archive claim {table} disappeared")
    item = tuple(metadata)
    width = len(columns)
    total = 0
    for column, kind, size in zip(columns, item[:width], item[width:], strict=True):
        expected = "integer" if column in {"target_number", "execution_epoch", "generation", "sequence",
                                           "owner_pid", "owner_start_ticks"} else "text"
        if (kind != expected and not (column in nullable and kind == "null")):
            raise WorkflowAuthorityError(f"archive claim {table} source has malformed SQL type")
        bound = MAX_WORKFLOW_PAYLOAD_BYTES if column in json_columns else 512
        if type(size) is not int or size > bound:
            raise WorkflowAuthorityError(f"archive claim {table} source exceeds field bound")
        total += size
    row = db.execute(f"SELECT {','.join(columns)} FROM {table} WHERE rowid=?", (rowid,)).fetchone()
    if row is None:
        raise WorkflowAuthorityError(f"archive claim {table} changed during read")
    return tuple(row), total


def _aware(value: object) -> datetime:
    if not isinstance(value, str) or len(value.encode()) > 64:
        raise WorkflowAuthorityError("archive claim time is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowAuthorityError("archive claim time is unreadable") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkflowAuthorityError("archive claim time lacks timezone")
    return parsed


def _canonical_json(raw: object) -> dict[str, object]:
    if not isinstance(raw, str):
        raise WorkflowAuthorityError("archive claim lifecycle payload is malformed")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise WorkflowAuthorityError("archive claim lifecycle payload is unreadable") from exc
    if (not isinstance(value, dict)
            or json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) != raw):
        raise WorkflowAuthorityError("archive claim lifecycle payload is not canonical")
    return value


def _observe(
    db: sqlite3.Connection, identity: ReviewRoundIdentity, max_source_bytes: int,
    indexed_identities: bool,
) -> ObservedReviewExecutionArchiveClaim:
    key = identity.binding.rejection.fence.key
    if not isinstance(key.target, PullRequestRef):
        raise WorkflowAuthorityError("archive claim requires a PR round")
    repository = key.repository
    preflight_ids = _matching_rowids(
        db, "dedupe_keys", ("namespace", "scope", "key"), (256, 256, 512),
        (_PREFLIGHT, repository, identity.round_id),
        indexed_identities=indexed_identities,
    )
    round_ids = _matching_rowids(
        db, "review_execution_rounds", ("repository", "target_kind", "round_id"), (256, 64, 512),
        (repository, "pr", identity.round_id),
        indexed_identities=indexed_identities,
    )
    if len(preflight_ids) != 1 or len(round_ids) != 1:
        raise WorkflowAuthorityError("archive claim lacks exact preflight or indexed round")
    sources: list[object] = []
    used = 0

    def read(table: str, rowid: int, columns: tuple[str, ...], **kwargs: object) -> tuple[object, ...]:
        nonlocal used
        row, size = _bounded_row(db, table, rowid, columns, **kwargs)
        used += size
        if used > max_source_bytes:
            raise WorkflowAuthorityError("archive claim exceeds cumulative source byte bound")
        sources.append((table, row))
        return row

    preflight = read(
        "dedupe_keys", preflight_ids[0],
        ("namespace", "scope", "key", "claimed_at", "reusable_after", "payload_json"),
        nullable=frozenset({"reusable_after"}), json_columns=frozenset({"payload_json"}),
    )
    if preflight[:3] != (_PREFLIGHT, repository, identity.round_id) or preflight[4] is not None:
        raise WorkflowAuthorityError("archive claim preflight identity or permanence changed")
    preflight_at = _aware(preflight[3])
    recorded = decode_workflow(preflight[5], tuple)
    if (len(recorded) != 3 or not isinstance(recorded[0], ReviewPreflightSelection)
            or not isinstance(recorded[1], str) or not recorded[1]
            or not isinstance(recorded[2], ReviewRoundPlan)
            or encode_workflow(recorded) != preflight[5]):
        raise WorkflowAuthorityError("archive claim preflight is not canonical")
    selection, owner_id, initial_plan = recorded
    if (selection.request_id != identity.round_id or selection.binding != identity.binding
            or initial_plan.round != identity or initial_plan.round.binding != selection.binding
            or selection.expected.fence.key != key):
        raise WorkflowAuthorityError("archive claim preflight disagrees with round")
    target = f"issue:{selection.claim_target}" if selection.claim_target > 0 else f"pr:{-selection.claim_target}"
    indexed = read(
        "review_execution_rounds", round_ids[0],
        ("repository", "target_kind", "target_number", "execution_epoch", "execution_episode",
         "generation", "sequence", "round_id", "claim_occurrence_id", "plan_json", "planning_json",
         "closure_json", "outcome_json"),
        nullable=frozenset({"closure_json", "outcome_json"}),
        json_columns=frozenset({"plan_json", "planning_json", "closure_json", "outcome_json"}),
    )
    expected_index = (repository, "pr", key.target.number, identity.binding.execution_epoch,
                      identity.binding.execution_episode, identity.binding.rejection.fence.generation,
                      identity.sequence, identity.round_id, selection.claim_occurrence_id)
    if indexed[:9] != expected_index:
        raise WorkflowAuthorityError("archive claim round index disagrees with preflight")
    current_plan = decode_workflow(indexed[9], ReviewRoundPlan)
    if (encode_workflow(current_plan) != indexed[9]
            or replace(initial_plan, stages=current_plan.stages,
                       submissions=current_plan.submissions) != current_plan):
        raise WorkflowAuthorityError("archive claim current plan changed")
    stage_prefix = review_stage_receipt_prefix(identity.round_id)
    stage_meta = db.execute(
        "SELECT rowid,typeof(key),typeof(payload_json),"
        f"{_octets('key')},{_octets('payload_json')} "
        "FROM dedupe_keys WHERE namespace=? AND scope=? AND key>=? AND key<? LIMIT ?",
        (REVIEW_STAGE_NAMESPACE, repository, stage_prefix, stage_prefix[:-1] + ";",
         MAX_REVIEW_STAGES + 1),
    ).fetchall()
    if len(stage_meta) != len(current_plan.stages):
        raise WorkflowAuthorityError("archive claim stage receipts disagree with current plan")
    stage_bytes = 0
    for metadata in stage_meta:
        _, key_type, payload_type, key_size, payload_size = tuple(metadata)
        if (key_type != "text" or payload_type != "text"
                or type(key_size) is not int or key_size > 512
                or type(payload_size) is not int or payload_size > MAX_WORKFLOW_PAYLOAD_BYTES):
            raise WorkflowAuthorityError("archive claim stage receipt exceeds field bound")
        stage_bytes += key_size + payload_size
    if used + 2 * stage_bytes > max_source_bytes:
        raise WorkflowAuthorityError("archive claim exceeds cumulative source byte bound")
    for metadata in stage_meta:
        stage = db.execute(
            "SELECT key,payload_json FROM dedupe_keys WHERE rowid=?", (metadata[0],),
        ).fetchone()
        if stage is None:
            raise WorkflowAuthorityError("archive claim stage receipt changed during read")
        sources.append(("review-round-stage", tuple(stage)))
    used += 2 * stage_bytes  # The canonical helper reads each stage payload again.
    require_review_stage_receipts(db, repository, current_plan)

    lifecycle_ids = _matching_rowids(
        db, "dedupe_keys", ("namespace", "scope", "key"), (256, 256, 512),
        (_LIFECYCLE, repository, f"{selection.claim_occurrence_id}:"), prefix_last=True,
        indexed_identities=indexed_identities,
    )
    events: dict[str, tuple[object, ...]] = {}
    for rowid in lifecycle_ids:
        row = read(
            "dedupe_keys", rowid,
            ("namespace", "scope", "key", "claimed_at", "reusable_after", "payload_json"),
            nullable=frozenset({"reusable_after"}), json_columns=frozenset({"payload_json"}),
        )
        prefix = f"{selection.claim_occurrence_id}:"
        if row[:2] != (_LIFECYCLE, repository) or not row[2].startswith(prefix) or row[4] is not None:
            raise WorkflowAuthorityError("archive claim lifecycle identity or permanence changed")
        event = row[2][len(prefix):]
        if event not in {"created", "released", "reclaimed"} or event in events:
            raise WorkflowAuthorityError("archive claim has extra or repeated lifecycle event")
        events[event] = row
    if "created" not in events:
        raise WorkflowAuthorityError("archive claim lacks creation receipt")
    created = _canonical_json(events["created"][5])
    expected_keys = {"role", "target", "leg", "owner_id", "owner_pid", "owner_start_ticks", "claimed_at", "source"}
    if (set(created) != expected_keys or created["role"] != "review" or created["target"] != target
            or created["leg"] not in ("REVIEW", "RE_REVIEW") or created["owner_id"] != owner_id
            or type(created["owner_pid"]) is not int or created["owner_pid"] <= 0
            or type(created["owner_start_ticks"]) is not int or created["owner_start_ticks"] <= 0
            or created["source"] not in ("admission", "v24-migration")
            or created["claimed_at"] != events["created"][3]):
        raise WorkflowAuthorityError("archive claim creation contradicts preflight")
    created_at = _aware(created["claimed_at"])
    if created_at > preflight_at:
        raise WorkflowAuthorityError("archive claim was created after its preflight")
    live_ids = _matching_rowids(
        db, "dispatch_claims", ("repository", "role", "occurrence_id"), (256, 64, 512),
        (repository, "review", selection.claim_occurrence_id),
        indexed_identities=indexed_identities,
    )
    terminal = tuple(event for event in ("released", "reclaimed") if event in events)
    if len(terminal) > 1 or (terminal and live_ids) or (not terminal and len(live_ids) != 1):
        raise WorkflowAuthorityError("archive claim has contradictory live or terminal authority")
    if terminal:
        event = terminal[0]
        ended = _canonical_json(events[event][5])
        if (ended != {name: value for name, value in created.items() if name != "source"}
                or _aware(events[event][3]) < created_at):
            raise WorkflowAuthorityError("archive claim terminal receipt disagrees with creation")
        kind: Literal["live", "released", "reclaimed"] = event
    else:
        live = read(
            "dispatch_claims", live_ids[0],
            ("repository", "role", "target", "leg", "owner_id", "owner_pid",
             "owner_start_ticks", "paths_json", "claimed_at", "occurrence_id"),
            json_columns=frozenset({"paths_json"}),
        )
        if (live[:2] != (repository, "review") or live[2:7] != (
                target, created["leg"], owner_id, created["owner_pid"], created["owner_start_ticks"])
                or live[8:] != (created["claimed_at"], selection.claim_occurrence_id)):
            raise WorkflowAuthorityError("archive claim current row disagrees with creation")
        try:
            paths = json.loads(live[7])
        except (TypeError, ValueError) as exc:
            raise WorkflowAuthorityError("archive claim current paths are unreadable") from exc
        if (not isinstance(paths, list) or any(not isinstance(path, str) for path in paths)
                or paths != sorted(set(paths))
                or json.dumps(paths, separators=(",", ":"), ensure_ascii=False) != live[7]):
            raise WorkflowAuthorityError("archive claim current paths are not canonical")
        kind = "live"
    digest = hashlib.sha256(json.dumps(sources, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    return ObservedReviewExecutionArchiveClaim(owner_id, selection.claim_occurrence_id, kind, digest, used)


def observe_review_execution_archive_claim(
    db: sqlite3.Connection, round_identity: ReviewRoundIdentity,
    *, max_source_bytes: int = MAX_ARCHIVE_CLAIM_SOURCE_BYTES,
    indexed_identities: bool = False,
) -> ObservedReviewExecutionArchiveClaim:
    """Observe exact archived claim ancestry in the caller's SQL transaction."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(round_identity, ReviewRoundIdentity)
            or type(indexed_identities) is not bool
            or type(max_source_bytes) is not int
            or not 1 <= max_source_bytes <= MAX_ARCHIVE_CLAIM_SOURCE_BYTES):
        raise ValueError("archive claim observation requires a transaction, typed round and bounded budget")
    getlimit = getattr(db, "getlimit", None)
    setlimit = getattr(db, "setlimit", None)
    if not callable(getlimit) or not callable(setlimit):
        raise WorkflowAuthorityError("archive claim observation requires SQLite length-limit APIs")
    category = sqlite3.SQLITE_LIMIT_LENGTH
    try:
        prior = getlimit(category)
        setlimit(category, min(prior, _SQL_LENGTH_LIMIT))
    except sqlite3.Error as exc:
        raise WorkflowAuthorityError("archive claim length guard is unavailable") from exc
    try:
        try:
            return _observe(db, round_identity, max_source_bytes, indexed_identities)
        except (sqlite3.Error, UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise WorkflowAuthorityError(f"archive claim SQL source is unreadable: {exc}") from exc
    finally:
        try:
            setlimit(category, prior)
        except sqlite3.Error as exc:
            raise WorkflowAuthorityError("archive claim length guard could not be restored") from exc
