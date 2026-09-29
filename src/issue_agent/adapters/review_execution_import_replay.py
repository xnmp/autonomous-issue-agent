"""Bounded SQL proof for a previously committed empty review execution import."""

from __future__ import annotations

from datetime import datetime
import hashlib
import sqlite3

from ..domain.review_activation import MAX_IMPORTED_PULL_REQUESTS
from ..domain.review_execution import (
    ActivatedReviewExecution, ImportedReviewExecutionOriginReceipt,
    ReviewExecutionOriginKind, same_execution_episode,
)
from ..domain.review_execution_import_plan import EmptyReviewExecutionImportPlan
from ..domain.workflow_identity import MAX_WORKFLOW_GATES, PullRequestRef, WorkflowFamily, WorkflowKey
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, encode_workflow
from .workflow_rows import WorkflowAuthorityError, load_review_workflow_row


_FAMILY = WorkflowFamily.REVIEW_EXECUTION.value
_ORIGIN = "review-execution-origin"
_MAX_COMPONENTS = MAX_IMPORTED_PULL_REQUESTS * 2
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
# Four codec payload columns can share one round row; reserve room for identity metadata.
_SQL_LENGTH_LIMIT = 8 * MAX_WORKFLOW_PAYLOAD_BYTES
_MAX_GLOBAL_ROWS = 65_536
_CENSUS_IDENTITIES = {
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


def _repository_payload_corpus_bytes(
    db: sqlite3.Connection, table: str, identity: str, repository: str,
    *, nullable: bool,
) -> int:
    # Count cells the canonical reader may revisit without fetching their bytes.
    payload_size = (
        "ifnull(octet_length(payload_json),0)"
        if sqlite3.sqlite_version_info >= (3, 43, 0)
        else "ifnull(length(CAST(payload_json AS BLOB)),0)"
    )
    identity_size = (
        f"ifnull(octet_length({identity}),0)"
        if sqlite3.sqlite_version_info >= (3, 43, 0)
        else f"ifnull(length(CAST({identity} AS BLOB)),0)"
    )
    rows = db.execute(
        f"SELECT typeof({identity}),{identity}=?,typeof(payload_json),"
        f"{identity_size},{payload_size} FROM {table} "
        f"WHERE lower(trim(CAST({identity} AS TEXT)))=lower(?) LIMIT ?",
        (repository, repository, _MAX_GLOBAL_ROWS + 1),
    ).fetchall()
    if len(rows) > _MAX_GLOBAL_ROWS:
        raise WorkflowAuthorityError(f"review execution import {table} corpus exceeds row bound")
    total = 0
    for row in rows:
        identity_type, exact, payload_type, identity_bytes, payload_bytes = tuple(row)
        if (identity_type != "text" or exact != 1
                or type(identity_bytes) is not int or identity_bytes > 512
                or payload_type not in ({"text", "null"} if nullable else {"text"})
                or type(payload_bytes) is not int
                or payload_bytes > MAX_WORKFLOW_PAYLOAD_BYTES):
            raise WorkflowAuthorityError(f"review execution import {table} corpus source is malformed")
        total += payload_bytes
        if total > _MAX_SOURCE_BYTES:
            raise WorkflowAuthorityError(f"review execution import {table} corpus exceeds byte bound")
    return total


def _latest_intent_source_bytes(
    db: sqlite3.Connection, repository: str, prs: set[int],
) -> int:
    # The canonical reader fetches its latest rearm row as a whole.
    size = (
        "octet_length" if sqlite3.sqlite_version_info >= (3, 43, 0)
        else "length"
    )
    def octets(column: str) -> str:
        return (f"ifnull({size}({column}),0)" if size == "octet_length"
                else f"ifnull(length(CAST({column} AS BLOB)),0)")
    query = (
        "SELECT typeof(intent_json),typeof(actor_json),"
        f"{octets('intent_json')},{octets('actor_json')} "
        "FROM workflow_rearms WHERE repository=? AND target_kind='pr' "
        "AND target_number=? ORDER BY to_generation DESC LIMIT 1"
    )
    total = 0
    for pr in prs:
        row = db.execute(query, (repository, pr)).fetchone()
        if row is None:
            continue
        intent_type, actor_type, intent_size, actor_size = tuple(row)
        if (intent_type != "text" or actor_type != "text"
                or type(intent_size) is not int or type(actor_size) is not int
                or intent_size > MAX_WORKFLOW_PAYLOAD_BYTES
                or actor_size > MAX_WORKFLOW_PAYLOAD_BYTES):
            raise WorkflowAuthorityError("review execution import latest rearm source exceeds bound")
        total += intent_size + actor_size
        if total > _MAX_SOURCE_BYTES:
            raise WorkflowAuthorityError("review execution import rearm sources exceed cumulative bound")
    return total


def _require_bounded_global_census(db: sqlite3.Connection, repository: str) -> None:
    # Indexed SQL equality misses Unicode whitespace and casefold aliases.
    # Limit the global pass, bound every identity cell before fetching it, and
    # compare using the same Python normalization as the domain boundary.
    for table, columns in _CENSUS_IDENTITIES.items():
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



def _verify_committed_empty_review_execution_import(
    db: sqlite3.Connection, plan: EmptyReviewExecutionImportPlan,
) -> bool:
    """Prove an exact historical import without consulting current external sources.

    False is reserved for a wholly absent execution activation and its related
    SQL records. Once authority exists, every mismatch is a hard conflict.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("review execution import replay requires an open SQL transaction")
    if not isinstance(plan, EmptyReviewExecutionImportPlan):
        raise ValueError("review execution import replay requires a typed plan")
    _require_bounded_global_census(db, plan.repository)
    repository = plan.repository
    authority = _table(db, "workflow_authority_epochs",
                       ("repository", "family", "epoch", "source_digest", "inventory_digest", "activated_at"),
                       repository, family=_FAMILY, limit=1)
    evidence = _table(db, "workflow_activation_evidence",
                      ("repository", "family", "epoch", "ceiling", "plan_digest"),
                      repository, family=_FAMILY, limit=1)
    imports = _table(db, "workflow_activation_imports",
                     ("repository", "family", "epoch", "target_kind", "target_number", "source_digest", "decision_json"),
                     repository, family=_FAMILY, payload_columns=frozenset({"decision_json"}))
    if sum(len(row[6].encode("utf-8")) for row in imports) > _MAX_SOURCE_BYTES:
        raise WorkflowAuthorityError("review execution import decisions exceed source byte bound")
    components = _table(db, "workflow_components",
                        ("repository", "target_kind", "target_number", "family", "epoch", "episode_id"),
                        repository, family=_FAMILY, limit=_MAX_COMPONENTS)
    origins = _table(db, "dedupe_keys",
                     ("namespace", "scope", "key", "claimed_at", "payload_json"),
                     repository, namespace=_ORIGIN, limit=_MAX_COMPONENTS,
                     payload_columns=frozenset({"payload_json"}))
    if (sum(len(row[6].encode("utf-8")) for row in imports)
            + sum(len(row[4].encode("utf-8")) for row in origins) > _MAX_SOURCE_BYTES):
        raise WorkflowAuthorityError("review execution import payloads exceed cumulative byte bound")
    round_source = db.execute(
        "SELECT 1 FROM review_execution_rounds "
        "WHERE lower(trim(CAST(repository AS TEXT)))=lower(?) LIMIT 1",
        (repository,),
    ).fetchone()
    if not any((authority, evidence, imports, components, origins, round_source)):
        return False
    if len(authority) != 1 or len(evidence) != 1 or len(imports) != len(plan.candidates):
        raise WorkflowAuthorityError("review execution import activation or membership changed")
    activated = authority[0][5]
    try:
        parsed = datetime.fromisoformat(activated)
    except (TypeError, ValueError) as exc:
        raise WorkflowAuthorityError("review execution import activation time is malformed") from exc
    if (parsed.tzinfo is None or parsed.utcoffset() is None
            or parsed < plan.planned_at
            or authority[0][:5] != (repository, _FAMILY, plan.execution_epoch,
                                     plan.source_digest, plan.inventory_digest)
            or evidence[0] != (repository, _FAMILY, plan.execution_epoch, 0, plan.plan_digest)):
        raise WorkflowAuthorityError("review execution import activation disagrees with plan")

    decisions = plan.decisions_at(plan.planned_at)
    decision_by_pr = dict(zip(plan.listing.numbers, decisions, strict=True))
    expected_imports = {
        candidate.pr: (repository, _FAMILY, plan.execution_epoch, "pr", candidate.pr,
                       plan.source_digest, encode_workflow(decision))
        for candidate, decision in zip(plan.candidates, decisions, strict=True)
    }
    if (len({row[4] for row in imports}) != len(imports)
            or any(expected_imports.get(row[4]) != row for row in imports)):
        raise WorkflowAuthorityError("review execution import decision or membership changed")

    component_by_pr: dict[int, tuple[object, ...]] = {}
    for row in components:
        if (row[:2] != (repository, "pr") or row[3:5] != (_FAMILY, plan.execution_epoch)
                or row[2] in component_by_pr):
            raise WorkflowAuthorityError("review execution import component identity changed")
        component_by_pr[row[2]] = row
    if not set(expected_imports).issubset(component_by_pr):
        raise WorkflowAuthorityError("review execution import component is missing")

    workflow_rows = _table(
        db, "workflows",
        ("repository", "target_kind", "target_number", "generation", "version", "episode_json"),
        repository, kind="pr", limit=_MAX_COMPONENTS,
        payload_columns=frozenset({"episode_json"}),
    )
    if (sum(len(row[6].encode("utf-8")) for row in imports)
            + sum(len(row[4].encode("utf-8")) for row in origins)
            + sum(len(row[5].encode("utf-8")) for row in workflow_rows) > _MAX_SOURCE_BYTES):
        raise WorkflowAuthorityError("review execution import workflow sources exceed cumulative byte bound")
    active_gates = _table(
        db, "workflow_gates",
        ("gate_id", "repository", "target_kind", "target_number",
         "opened_generation", "cause_key", "state", "payload_json"),
        repository, kind="pr", active_gates=True, limit=_MAX_GLOBAL_ROWS,
        payload_columns=frozenset({"payload_json"}),
    )
    if (sum(len(row[6].encode("utf-8")) for row in imports)
            + sum(len(row[4].encode("utf-8")) for row in origins)
            + sum(len(row[5].encode("utf-8")) for row in workflow_rows)
            + sum(len(row[7].encode("utf-8")) for row in active_gates) > _MAX_SOURCE_BYTES):
        raise WorkflowAuthorityError("review execution import linked sources exceed cumulative byte bound")
    gates_by_pr: dict[int, int] = {}
    for gate in active_gates:
        if (gate[1:3] != (repository, "pr") or gate[3] not in component_by_pr
                or gate[6] not in ("open", "rearming")):
            raise WorkflowAuthorityError("review execution import active gate identity changed")
        gates_by_pr[gate[3]] = gates_by_pr.get(gate[3], 0) + 1
        if gates_by_pr[gate[3]] > MAX_WORKFLOW_GATES:
            raise WorkflowAuthorityError("review execution import active gate count exceeds bound")
    latest_intent_bytes = _latest_intent_source_bytes(db, repository, set(component_by_pr))
    dedupe_bytes = _repository_payload_corpus_bytes(
        db, "dedupe_keys", "scope", repository, nullable=True,
    )
    evidence_bytes = _repository_payload_corpus_bytes(
        db, "workflow_evidence", "repository", repository, nullable=False,
    )
    if (latest_intent_bytes + dedupe_bytes + evidence_bytes
            + sum(len(row[7].encode("utf-8")) for row in active_gates)
            + sum(len(row[5].encode("utf-8")) for row in workflow_rows)
            + sum(len(row[6].encode("utf-8")) for row in imports) > _MAX_SOURCE_BYTES):
        raise WorkflowAuthorityError("review execution import linked sources exceed cumulative byte bound")
    workflow_numbers = [row[2] for row in workflow_rows]
    if (any(row[:2] != (repository, "pr") for row in workflow_rows)
            or len(workflow_numbers) != len(set(workflow_numbers))
            or set(workflow_numbers) != set(component_by_pr)):
        raise WorkflowAuthorityError("review execution import workflow membership changed")

    orphan_round = db.execute(
        "SELECT 1 FROM review_execution_rounds AS r "
        "WHERE lower(trim(CAST(r.repository AS TEXT)))=lower(?) "
        "AND (typeof(r.repository)!='text' OR r.repository!=? "
        "OR typeof(r.target_number)!='integer' OR typeof(r.execution_epoch)!='integer' "
        "OR NOT EXISTS (SELECT 1 FROM workflow_components AS c "
        "WHERE c.repository=r.repository AND c.target_kind='pr' "
        "AND c.target_number=r.target_number AND c.family=? "
        "AND c.epoch=r.execution_epoch AND c.episode_id=r.execution_episode)) LIMIT 1",
        (repository, repository, _FAMILY),
    ).fetchone()
    if orphan_round is not None:
        raise WorkflowAuthorityError("review execution import has an orphan round")

    expected_origins: dict[str, tuple[object, ...]] = {}
    for decision in decisions:
        encoded = encode_workflow(decision)
        receipt = ImportedReviewExecutionOriginReceipt(
            decision.origin, decision.checkpoint, plan.plan_digest, plan.source_digest,
            plan.inventory_digest, hashlib.sha256(encoded.encode()).hexdigest(),
        )
        expected_origins[decision.origin.evidence_id] = (
            _ORIGIN, repository, decision.origin.evidence_id, activated, encode_workflow(receipt),
        )
    origin_by_id = {row[2]: row for row in origins}
    if len(origin_by_id) != len(origins) or any(
        origin_by_id.get(key) != value for key, value in expected_origins.items()
    ):
        raise WorkflowAuthorityError("review execution import origin receipt changed")

    seen_origin_ids: set[str] = set()
    for pr, component in component_by_pr.items():
        snapshot = load_review_workflow_row(db, WorkflowKey(repository, PullRequestRef(pr)))
        if snapshot is None or not isinstance(snapshot.execution, ActivatedReviewExecution):
            raise WorkflowAuthorityError("review execution import current authority is absent")
        execution = snapshot.execution
        if component[5] != execution.binding.execution_episode:
            raise WorkflowAuthorityError("review execution import component disagrees with workflow")
        seen_origin_ids.add(execution.origin.evidence_id)
        if pr in expected_imports:
            decision = decision_by_pr[pr]
            if (execution.origin != decision.origin
                    or not same_execution_episode(execution.binding, decision.checkpoint.state.binding)):
                raise WorkflowAuthorityError("review execution import ancestry changed")
        elif execution.origin.kind is not ReviewExecutionOriginKind.INITIALIZED:
            raise WorkflowAuthorityError("review execution import has an unplanned imported component")
    if seen_origin_ids != set(origin_by_id):
        raise WorkflowAuthorityError("review execution import has a missing or extra origin")
    return True


def verify_committed_empty_review_execution_import(
    db: sqlite3.Connection, plan: EmptyReviewExecutionImportPlan,
) -> bool:
    """Prove a committed import with a temporary SQLite row/value read ceiling.

    SQLite's per-connection length limit also guards linked authority sources
    read by the canonical workflow loader before its own payload checks.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("review execution import replay requires an open SQL transaction")
    if not isinstance(plan, EmptyReviewExecutionImportPlan):
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
            return _verify_committed_empty_review_execution_import(db, plan)
        except sqlite3.Error as exc:
            raise WorkflowAuthorityError("review execution import SQL source exceeds length guard or is unreadable") from exc
    finally:
        try:
            setlimit(category, prior)
        except sqlite3.Error as exc:
            raise WorkflowAuthorityError("review execution import SQL length guard could not be restored") from exc
