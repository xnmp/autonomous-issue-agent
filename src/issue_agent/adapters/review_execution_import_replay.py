"""Exact SQL retry of a mixed import; later unrelated history is not re-audited."""

from __future__ import annotations

from datetime import datetime
import hashlib
import sqlite3

from ..domain.review_activation import MAX_IMPORTED_PULL_REQUESTS
from ..domain.review_execution import (
    CurrentReviewExecution, ImportedReviewExecutionOriginReceipt,
    BlockedReviewExecutionMigration, same_execution_episode,
)
from ..domain.review_execution_import_plan import ReviewExecutionImportPlan
from ..domain.workflow_identity import PullRequestRef, WorkflowFamily, WorkflowKey
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, encode_workflow
from .workflow_rows import (
    WorkflowAuthorityError, load_review_workflow_row, load_review_execution_import_decision,
    _read_execution_origin,
)


_FAMILY = WorkflowFamily.REVIEW_EXECUTION.value
_ORIGIN = "review-execution-origin"
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
# Four codec payload columns can share one round row; reserve room for identity metadata.
_SQL_LENGTH_LIMIT = 8 * MAX_WORKFLOW_PAYLOAD_BYTES
_MAX_GLOBAL_ROWS = 65_536
_UNPUBLISHED_SCOPE_IDENTITIES = {
    "workflow_authority_epochs": ("repository", "family"),
    "workflow_activation_evidence": ("repository", "family"),
    "workflow_activation_imports": ("repository", "family", "target_kind"),
    "workflow_components": ("repository", "family", "target_kind"),
    "dedupe_keys": ("scope", "namespace"),
    "review_execution_rounds": ("repository", "target_kind"),
    "workflows": ("repository", "target_kind"),
    "workflow_gates": ("repository", "target_kind"),
    "workflow_rearms": ("repository", "target_kind"),
    "workflow_evidence": ("repository", "target_kind"),
}



def _selected(
    db: sqlite3.Connection, table: str, columns: tuple[str, ...], repository: str,
    *, family: str | None = None, namespace: str | None = None, kind: str | None = None,
    active_gates: bool = False, limit: int = MAX_IMPORTED_PULL_REQUESTS,
) -> list[tuple[object, ...]]:
    """Select bounded metadata before retrieving any untrusted payload values."""
    predicate = "lower(trim(CAST(repository AS TEXT)))=lower(?)"
    params: list[object] = [repository]
    if namespace is not None:
        predicate = "lower(trim(CAST(scope AS TEXT)))=lower(?) AND lower(trim(CAST(namespace AS TEXT)))=lower(?)"
        params = [repository, namespace]
    elif family is not None:
        predicate += " AND lower(trim(CAST(family AS TEXT)))=lower(?)"
        params.append(family)
    elif kind is not None:
        predicate += " AND lower(trim(CAST(target_kind AS TEXT)))=lower(?)"
        params.append(kind)
    # SQLite 3.43+ derives octet_length from record metadata. Older runtimes
    # retain the weaker CAST read until the platform version floor advances.
    sizes = (
        f"ifnull(octet_length({name}),0)" if sqlite3.sqlite_version_info >= (3, 43, 0)
        else f"ifnull(length(CAST({name} AS BLOB)),0)"
        for name in columns
    )
    if active_gates:
        predicate += " AND state!='closed'"
    projection = ",".join(("rowid", *(f"typeof({name})" for name in columns), *sizes))
    try:
        rows = db.execute(
            f"SELECT {projection} FROM {table} WHERE {predicate} LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
    except (sqlite3.Error, UnicodeError) as exc:
        raise WorkflowAuthorityError(f"review execution import {table} metadata is unreadable") from exc
    if len(rows) > limit:
        raise WorkflowAuthorityError(f"review execution import {table} exceeds row bound")
    return [tuple(row) for row in rows]


def _read(
    db: sqlite3.Connection, table: str, columns: tuple[str, ...], metadata: tuple[object, ...],
    *, payload_columns: frozenset[str] = frozenset(),
) -> tuple[object, ...]:
    width = len(columns)
    types = metadata[1:1 + width]
    sizes = metadata[1 + width:]
    for name, kind, size in zip(columns, types, sizes, strict=True):
        expected = "integer" if name in {"epoch", "ceiling", "target_number", "generation", "version", "opened_generation"} else "text"
        bound = MAX_WORKFLOW_PAYLOAD_BYTES if name in payload_columns else 512
        if kind != expected or type(size) is not int or size > bound:
            raise WorkflowAuthorityError(f"review execution import {table} has malformed SQL source")
    try:
        row = db.execute(f"SELECT {','.join(columns)} FROM {table} WHERE rowid=?", (metadata[0],)).fetchone()
    except (sqlite3.Error, UnicodeError) as exc:
        raise WorkflowAuthorityError(f"review execution import {table} source is unreadable") from exc
    if row is None:
        raise WorkflowAuthorityError(f"review execution import {table} changed during replay")
    return tuple(row)


def _table(
    db: sqlite3.Connection, table: str, columns: tuple[str, ...], repository: str,
    *, family: str | None = None, namespace: str | None = None, kind: str | None = None,
    active_gates: bool = False, limit: int = MAX_IMPORTED_PULL_REQUESTS,
    payload_columns: frozenset[str] = frozenset(),
) -> list[tuple[object, ...]]:
    metadata = _selected(db, table, columns, repository, family=family,
                         namespace=namespace, kind=kind, active_gates=active_gates, limit=limit)
    total = sum(sum(item[1 + len(columns):]) for item in metadata)
    if total > _MAX_SOURCE_BYTES:
        raise WorkflowAuthorityError(f"review execution import {table} exceeds source byte bound")
    return [_read(db, table, columns, item, payload_columns=payload_columns) for item in metadata]


def _require_bounded_unpublished_scope(db: sqlite3.Connection, repository: str) -> None:
    # Indexed SQL equality misses Unicode whitespace and casefold aliases.
    # Limit the global pass, bound every identity cell before fetching it, and
    # compare using the same Python normalization as the domain boundary.
    for table, columns in _UNPUBLISHED_SCOPE_IDENTITIES.items():
        sizes = tuple(
            (f"ifnull(octet_length({column}),0)"
             if sqlite3.sqlite_version_info >= (3, 43, 0)
             else f"ifnull(length(CAST({column} AS BLOB)),0)")
            for column in columns
        )
        projection = ",".join((
            "rowid",
            *(f"typeof({column})" for column in columns),
            *sizes,
            *(f"CASE WHEN {size}<=? THEN CAST({column} AS TEXT) END"
              for column, size in zip(columns, sizes, strict=True)),
        ))
        limits = tuple(256 if column in {"repository", "scope", "namespace"} else 64
                       for column in columns)
        try:
            rows = db.execute(
                f"SELECT {projection} FROM {table} ORDER BY rowid LIMIT ?",
                (*limits, _MAX_GLOBAL_ROWS + 1),
            ).fetchall()
        except (sqlite3.Error, UnicodeError) as exc:
            raise WorkflowAuthorityError(
                f"review execution import {table} identity census is unreadable"
            ) from exc
        if len(rows) > _MAX_GLOBAL_ROWS:
            raise WorkflowAuthorityError(
                f"review execution import {table} exceeds global census bound"
            )
        width = len(columns)
        for row in rows:
            item = tuple(row)
            types, lengths, values = (
                item[1:1 + width], item[1 + width:1 + 2 * width],
                item[1 + 2 * width:],
            )
            if any(type(length) is not int or length > limit or not isinstance(value, str)
                   for length, limit, value in zip(lengths, limits, values, strict=True)):
                raise WorkflowAuthorityError(
                    f"review execution import {table} identity exceeds bound"
                )
            identity = values[0]
            if identity.strip().casefold() != repository:
                continue
            if types[0] != "text" or identity != repository:
                raise WorkflowAuthorityError(
                    f"review execution import {table} repository identity is aliased"
                )
            for column, kind, value in zip(columns[1:], types[1:], values[1:], strict=True):
                normalized = value.strip().casefold()
                expected = (_FAMILY if column == "family" else _ORIGIN if column == "namespace"
                            else "pr")
                if normalized == expected and (kind != "text" or value != expected):
                    raise WorkflowAuthorityError(
                        f"review execution import {table} {column} identity is aliased"
                    )



def _verify_committed_review_execution_import(db: sqlite3.Connection, plan: ReviewExecutionImportPlan) -> bool:
    repository = plan.repository
    authority = _table(db, "workflow_authority_epochs",
                       ("repository", "family", "epoch", "source_digest", "inventory_digest", "activated_at"),
                       repository, family=_FAMILY, limit=1)
    if not authority:
        # An absent epoch is safe to publish only after the existing whole-scope
        # identity inventory. Successful retries never revisit that inventory.
        _require_bounded_unpublished_scope(db, repository)
        for table, predicate, params in (
            ("workflow_activation_evidence", "repository=? AND family=?", (repository, _FAMILY)),
            ("workflow_activation_imports", "repository=? AND family=?", (repository, _FAMILY)),
            ("workflow_components", "repository=? AND family=?", (repository, _FAMILY)),
            ("dedupe_keys", "scope=? AND namespace=?", (repository, _ORIGIN)),
            ("review_execution_rounds", "repository=?", (repository,)),
        ):
            if db.execute(f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1", params).fetchone() is not None:
                raise WorkflowAuthorityError("absent execution activation retains incompatible authority")
        return False
    evidence = _table(db, "workflow_activation_evidence",
                      ("repository", "family", "epoch", "ceiling", "plan_digest"),
                      repository, family=_FAMILY, limit=1)
    imports = _table(db, "workflow_activation_imports",
                     ("repository", "family", "epoch", "target_kind", "target_number", "source_digest", "decision_json"),
                     repository, family=_FAMILY, payload_columns=frozenset({"decision_json"}))
    if len(authority) != 1 or len(evidence) != 1 or len(imports) != len(plan.candidates):
        raise WorkflowAuthorityError("review execution import activation or membership changed")
    activated = authority[0][5]
    try:
        parsed = datetime.fromisoformat(activated)
    except (TypeError, ValueError) as exc:
        raise WorkflowAuthorityError("review execution import activation time is malformed") from exc
    if (parsed.tzinfo is None or parsed.utcoffset() is None or parsed < plan.planned_at
            or authority[0][:5] != (repository, _FAMILY, plan.execution_epoch, plan.source_digest, plan.inventory_digest)
            or evidence[0] != (repository, _FAMILY, plan.execution_epoch, 0, plan.plan_digest)):
        raise WorkflowAuthorityError("review execution import activation disagrees with plan")
    decisions = plan.decisions_at(plan.planned_at)
    expected = {
        candidate.pr: (repository, _FAMILY, plan.execution_epoch, "pr", candidate.pr,
                       plan.source_digest, encode_workflow(decision))
        for candidate, decision in zip(plan.candidates, decisions, strict=True)
    }
    if len({row[4] for row in imports}) != len(imports) or any(expected.get(row[4]) != row for row in imports):
        raise WorkflowAuthorityError("review execution import decision or membership changed")
    for candidate, decision in zip(plan.candidates, decisions, strict=True):
        key = WorkflowKey(repository, PullRequestRef(candidate.pr))
        if load_review_execution_import_decision(db, key) != decision:
            raise WorkflowAuthorityError("review execution original target decision changed")
        snapshot = load_review_workflow_row(db, key)
        if isinstance(decision, BlockedReviewExecutionMigration):
            if snapshot is None or snapshot.execution != decision:
                raise WorkflowAuthorityError("review execution migration blocker changed")
            continue
        if snapshot is None or not isinstance(snapshot.execution, CurrentReviewExecution):
            raise WorkflowAuthorityError("review execution import current authority is absent")
        execution = snapshot.execution
        if (execution.origin != decision.origin
                or not same_execution_episode(execution.binding, decision.checkpoint.state.binding)):
            raise WorkflowAuthorityError("review execution import ancestry changed")
        encoded = encode_workflow(decision)
        receipt = ImportedReviewExecutionOriginReceipt(
            decision.origin, decision.checkpoint, plan.plan_digest, plan.source_digest,
            plan.inventory_digest, hashlib.sha256(encoded.encode()).hexdigest(),
        )
        stored, claimed_at = _read_execution_origin(db, (repository, "pr", candidate.pr), execution)
        if stored != receipt or claimed_at != parsed:
            raise WorkflowAuthorityError("review execution import origin receipt changed")
    return True


def verify_committed_review_execution_import(
    db: sqlite3.Connection, plan: ReviewExecutionImportPlan,
) -> bool:
    """Prove a committed import with a temporary SQLite row/value read ceiling.

    SQLite's per-connection length limit also guards linked authority sources
    read by the canonical workflow loader before its own payload checks.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("review execution import replay requires an open SQL transaction")
    if not isinstance(plan, ReviewExecutionImportPlan):
        raise ValueError("review execution import replay requires a typed plan")
    getlimit = getattr(db, "getlimit", None)
    setlimit = getattr(db, "setlimit", None)
    if not callable(getlimit) or not callable(setlimit):
        raise WorkflowAuthorityError(
            "review execution import requires SQLite length-limit APIs available in Python 3.11+"
        )
    category = sqlite3.SQLITE_LIMIT_LENGTH
    try:
        prior = getlimit(category)
        setlimit(category, min(prior, _SQL_LENGTH_LIMIT))
    except sqlite3.Error as exc:
        raise WorkflowAuthorityError("review execution import SQL length guard is unavailable") from exc
    try:
        try:
            return _verify_committed_review_execution_import(db, plan)
        except sqlite3.Error as exc:
            raise WorkflowAuthorityError("review execution import SQL source exceeds length guard or is unreadable") from exc
    finally:
        try:
            setlimit(category, prior)
        except sqlite3.Error as exc:
            raise WorkflowAuthorityError("review execution import SQL length guard could not be restored") from exc


def original_review_execution_plan(plan: ReviewExecutionImportPlan) -> dict[str, object]:
    """Transport the complete immutable plan without widening authority payloads."""
    if not isinstance(plan, ReviewExecutionImportPlan):
        raise ValueError("original execution plan requires a typed plan")
    value = {
        "format": 1, "repository": plan.repository, "listing": list(plan.listing.numbers),
        "execution_epoch": plan.execution_epoch, "source_set_digest": plan.source_set_digest,
        "retained_digest": plan.retained_digest, "inventory_digest": plan.inventory_digest,
        "planned_at": plan.planned_at.isoformat(), "policy": encode_workflow(plan.policy),
        "candidates": [encode_workflow(candidate) for candidate in plan.candidates],
        "plan_digest": plan.plan_digest,
    }
    import json
    if len(json.dumps(value, ensure_ascii=True).encode()) > _MAX_SOURCE_BYTES:
        raise WorkflowAuthorityError("original execution plan exceeds the source byte bound")
    return value


def read_original_review_execution_plan(path: str) -> ReviewExecutionImportPlan:
    """Read a bounded saved shadow report; each target uses the closed codec."""
    import json
    import os
    import stat
    from ..domain.review_activation import OpenPullRequestsListed
    from ..domain.review_execution import ReviewExecutionPolicy
    from ..domain.review_execution_import_plan import EligibleZeroHistoryReviewImport
    from ..domain.review_execution_migration import ReviewImportHistoryBlocked
    from .workflow_codec import decode_workflow, _unique_object

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as saved:
            metadata = os.fstat(saved.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SOURCE_BYTES:
                raise ValueError("original execution plan file exceeds the source bound or is not regular")
            raw = saved.read(_MAX_SOURCE_BYTES + 1)
        if len(raw) > _MAX_SOURCE_BYTES:
            raise ValueError("original execution plan exceeds the source bound")
        report = json.loads(raw, object_pairs_hook=_unique_object)
        value = report.get("original_plan", report) if isinstance(report, dict) else report
        names = {"format", "repository", "listing", "execution_epoch", "source_set_digest",
                 "retained_digest", "inventory_digest", "planned_at", "policy", "candidates", "plan_digest"}
        if (not isinstance(value, dict) or set(value) != names or type(value["format"]) is not int
                or value["format"] != 1 or not isinstance(value["listing"], list)
                or not isinstance(value["candidates"], list) or len(value["candidates"]) > MAX_IMPORTED_PULL_REQUESTS):
            raise ValueError("original execution plan has unsupported fields or target bound")
        plan = ReviewExecutionImportPlan(
            value["repository"], OpenPullRequestsListed(tuple(value["listing"])), value["execution_epoch"],
            value["source_set_digest"], value["retained_digest"], value["inventory_digest"],
            tuple(decode_workflow(item, EligibleZeroHistoryReviewImport | ReviewImportHistoryBlocked)
                  for item in value["candidates"]), datetime.fromisoformat(value["planned_at"]),
            decode_workflow(value["policy"], ReviewExecutionPolicy),
        )
        if plan.plan_digest != value["plan_digest"]:
            raise ValueError("original execution plan digest disagrees with its complete decisions")
        return plan
    except (TypeError, ValueError, KeyError, RecursionError, UnicodeError) as exc:
        raise WorkflowAuthorityError(f"original execution plan is malformed: {exc}") from exc
