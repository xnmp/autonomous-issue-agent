"""Bounded global pages of recorded review submission registrations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime

from ..domain.review_execution_activation import MAX_SQL_ROWID, MIN_SQL_ROWID
from ..domain.review_lifecycle_archive import (
    MAX_REVIEW_LIFECYCLE_PAGE_BYTES, MAX_REVIEW_LIFECYCLE_SOURCE_PAGE,
)
from ..domain.review_lifecycle_registration import (
    BlockedReviewLifecycleRegistrationPage, ObservedReviewLifecycleRegistrationPage,
    ReviewLifecycleRegistrationSource, LinkedReviewLifecycleRegistration,
)
from ..domain.review_preflight import ReviewPreflightSelection
from ..domain.contracts import CommandId, EffectReceipt, EffectStatus
from ..domain.review_rounds import (
    MAX_REVIEW_STAGES, ReviewRoundPlan, ReviewSubmissionKind,
    ReviewSubmissionRegistration,
)
from ..domain.workflow_identity import canonical_workflow_repository
from .review_archive_source_versions import read_review_archive_source_versions
from .review_execution_activation import (
    _DEDUPE_SOURCE_COLUMNS, _read_recording_source, _reject_nonfinite,
    _unique_receipt_object, _verified_review_stage_sources,
    read_checkpoint_round_row_source,
)
from .review_lifecycle_archive import _size_expression, _submission_at_rowid
from .control_store import _parse_stamp, _receipt_digest
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_SUBMISSION_NAMESPACE, REVIEW_SUBMISSION_INDEX_NAMESPACE,
    WorkflowAuthorityError, review_stage_receipt_prefix,
    review_submission_receipt_key,
)


def _bounded_recording_source(
    db: sqlite3.Connection, namespace: str, repository: str, key: str,
    remaining: int,
) -> tuple[dict[str, str | None], str, int, int]:
    sizes = tuple(_size_expression(column) for column in _DEDUPE_SOURCE_COLUMNS)
    metadata = db.execute(
        f"SELECT rowid AS source_rowid, {'+'.join(sizes)} AS source_bytes FROM dedupe_keys "
        "WHERE namespace=? AND scope=? AND key=?",
        (namespace, repository, key),
    ).fetchone()
    if metadata is None or type(metadata["source_bytes"]) is not int:
        raise ValueError(f"{namespace} source is absent")
    size = metadata["source_bytes"]
    if size > remaining:
        raise ValueError(f"{namespace} source exceeds the link byte bound")
    fields, digest = _read_recording_source(db, namespace, repository, key)
    return dict(fields), digest, size, metadata["source_rowid"]


def _round_source_sql_bytes(fields: tuple[tuple[str, str | int | None], ...]) -> int:
    return sum(
        len(str(value).encode()) if type(value) is int
        else len(value.encode("utf-8", "surrogatepass"))
        for _, value in fields if value is not None
    )


def read_review_lifecycle_registration_link(
    db: sqlite3.Connection, source: ReviewLifecycleRegistrationSource,
    *, max_source_bytes: int = MAX_REVIEW_LIFECYCLE_PAGE_BYTES,
) -> LinkedReviewLifecycleRegistration:
    """Recheck one recorded registration against its command and round plan."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(source, ReviewLifecycleRegistrationSource)
            or type(max_source_bytes) is not int
            or not 1 <= max_source_bytes <= MAX_REVIEW_LIFECYCLE_PAGE_BYTES):
        raise ValueError("registration link requires a transaction and bounded source")
    registration = source.registration
    repository = registration.round.binding.rejection.fence.key.repository
    command_id = registration.submission.command_id
    try:
        recorded, digest, source_bytes, rowid = _bounded_recording_source(
            db, REVIEW_SUBMISSION_NAMESPACE, repository, command_id, max_source_bytes,
        )
        if (rowid != source.source_rowid or digest != source.source_sha256
                or source_bytes != source.source_bytes
                or decode_workflow(recorded["payload_json"], ReviewSubmissionRegistration)
                != registration):
            raise ValueError("registration changed since its source page")
        index_key = review_submission_receipt_key(registration.round.round_id, command_id)
        indexed, index_digest, index_bytes, _ = _bounded_recording_source(
            db, REVIEW_SUBMISSION_INDEX_NAMESPACE, repository, index_key,
            max_source_bytes - source_bytes,
        )
        source_bytes += index_bytes
        if (recorded["payload_json"] != indexed["payload_json"]
                or recorded["claimed_at"] != indexed["claimed_at"]
                or recorded["reusable_after"] is not None
                or indexed["reusable_after"] is not None):
            raise ValueError("registration and round index disagree")
        preflight, preflight_digest, preflight_bytes, _ = _bounded_recording_source(
            db, "review-round-preflight", repository, registration.round.round_id,
            max_source_bytes - source_bytes,
        )
        source_bytes += preflight_bytes
        recorded_preflight = decode_workflow(preflight["payload_json"], tuple)
        round_source = read_checkpoint_round_row_source(
            db, registration.round, max_source_bytes=max_source_bytes - source_bytes,
        )
        source_bytes += _round_source_sql_bytes(round_source.fields)
        plan = decode_workflow(dict(round_source.fields)["plan_json"], ReviewRoundPlan)
        if (len(recorded_preflight) != 3
                or encode_workflow(recorded_preflight) != preflight["payload_json"]
                or not isinstance(recorded_preflight[0], ReviewPreflightSelection)
                or recorded_preflight[0].request_id != registration.round.round_id
                or recorded_preflight[0].claim_occurrence_id != registration.claim_occurrence_id
                or recorded_preflight[0].binding != registration.round.binding
                or recorded_preflight[1] != registration.owner_id
                or not isinstance(recorded_preflight[2], ReviewRoundPlan)
                or replace(
                    recorded_preflight[2], stages=plan.stages, submissions=plan.submissions,
                ) != plan
                or registration.submission not in plan.submissions
                or dict(round_source.fields)["claim_occurrence_id"]
                != registration.claim_occurrence_id):
            raise ValueError("registration disagrees with its round plan")
        aliases = db.execute(
            "SELECT rowid,typeof(command_id) AS identity_type "
            "FROM lifecycle_commands WHERE command_id IN (?,CAST(? AS BLOB)) LIMIT 2",
            (command_id, command_id),
        ).fetchall()
        if len(aliases) != 1 or aliases[0]["identity_type"] != "text":
            raise ValueError("registered lifecycle command is absent or aliased")
        command_source, command_bytes = _submission_at_rowid(
            db, repository, aliases[0]["rowid"], max_source_bytes - source_bytes,
        )
        source_bytes += command_bytes
        command = dict(command_source.command_fields)
        if command_source.receipt_fields is not None:
            effect = dict(command_source.receipt_fields)
            receipt = EffectReceipt(
                CommandId(command_id), effect["executor_identity"],
                _parse_stamp(effect["started_at"]), _parse_stamp(effect["completed_at"]),
                EffectStatus(effect["status"]), effect["remote_object_id"],
                effect["remote_revision"], effect["error"],
            )
            if (command["attempts"] < 1
                    or command["attempt_owner"] != receipt.executor_identity
                    or effect["receipt_digest"] != _receipt_digest(receipt)):
                raise ValueError("registered review effect lacks its paid attempt or digest")
        if (command["command_id"] != command_id
                or command["target"]
                != f"pr:{registration.round.binding.rejection.fence.key.target.number}"
                or command["expected_revision"] != plan.head_sha
                or command["config_version"] != registration.config_version
                or command["executor_identity"] != registration.owner_id
                or command["idempotency_key"] != registration.idempotency_key
                or command["payload_digest"] != registration.command_digest
                or command["workflow_binding_json"]
                != encode_workflow(registration.round.binding.rejection)
                or hashlib.sha256((
                    command["payload_json"] + "\n" + command["workflow_binding_json"]
                ).encode()).hexdigest() != command["payload_digest"]):
            raise ValueError("registered lifecycle command disagrees with its round")
        payload = json.loads(
            command["payload_json"], object_pairs_hook=_unique_receipt_object,
            parse_constant=_reject_nonfinite,
        )
        if (not isinstance(payload, dict) or payload.get("head") != plan.head_sha
                or payload.get("pr") != registration.round.binding.rejection.fence.key.target.number):
            raise ValueError("registered lifecycle payload disagrees with its round")
        if registration.submission.kind is ReviewSubmissionKind.PAID and not plan.stages:
            raise ValueError("paid review submission lacks a planned stage")
        if registration.submission.kind is ReviewSubmissionKind.COPIED and plan.stages:
            effect = dict(command_source.receipt_fields) if command_source.receipt_fields else None
            if (command["state"] != "permanent_failure" or command["attempts"] < 1
                    or effect is None or effect["status"] != "permanent_failure"
                    or effect["remote_object_id"] is not None):
                raise ValueError("copied review with later stages lacks definitive refusal")
        prefix = review_stage_receipt_prefix(plan.round.round_id)
        sizes = tuple(_size_expression(column) for column in _DEDUPE_SOURCE_COLUMNS)
        stage_rows = db.execute(
            f"SELECT {'+'.join(sizes)} AS source_bytes FROM dedupe_keys "
            "WHERE namespace='review-round-stage' AND scope=? AND key>=? AND key<? "
            "ORDER BY key LIMIT ?",
            (repository, prefix, prefix[:-1] + ";", MAX_REVIEW_STAGES + 1),
        ).fetchall()
        if (len(stage_rows) != len(plan.stages)
                or any(type(row["source_bytes"]) is not int for row in stage_rows)):
            raise ValueError("review stage receipts disagree with the round")
        stage_bytes = sum(row["source_bytes"] for row in stage_rows)
        if stage_bytes > max_source_bytes - source_bytes:
            raise ValueError("review stage receipts exceed the link byte bound")
        stage_digest = _verified_review_stage_sources(db, repository, plan)
        source_bytes += stage_bytes
        return LinkedReviewLifecycleRegistration(
            source, command_source, round_source, index_digest,
            preflight_digest, stage_digest, source_bytes,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError,
            sqlite3.Error, OverflowError, WorkflowAuthorityError) as exc:
        raise WorkflowAuthorityError("registration cannot be linked to its command and round") from exc


def read_review_lifecycle_registration_page(
    db: sqlite3.Connection, repository: str, *, after_rowid: int | None = None,
    upper_rowid: int | None = None,
    limit: int = MAX_REVIEW_LIFECYCLE_SOURCE_PAGE,
    max_source_bytes: int = MAX_REVIEW_LIFECYCLE_PAGE_BYTES,
) -> ObservedReviewLifecycleRegistrationPage | BlockedReviewLifecycleRegistrationPage:
    """Read one fixed global dedupe page; a later cursor must reconcile its sources."""
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or canonical_workflow_repository(repository) != repository
            or after_rowid is not None and (
                type(after_rowid) is not int
                or not MIN_SQL_ROWID <= after_rowid <= MAX_SQL_ROWID
            ) or upper_rowid is not None and (
                type(upper_rowid) is not int
                or not MIN_SQL_ROWID <= upper_rowid <= MAX_SQL_ROWID
            ) or after_rowid is not None and (
                upper_rowid is None or after_rowid > upper_rowid
            ) or type(limit) is not int or not 1 <= limit <= MAX_REVIEW_LIFECYCLE_SOURCE_PAGE
            or type(max_source_bytes) is not int
            or not 1 <= max_source_bytes <= MAX_REVIEW_LIFECYCLE_PAGE_BYTES):
        raise ValueError("registration page requires a transaction and bounded cursor")
    version = read_review_archive_source_versions(db, repository).round_receipts
    if upper_rowid is None:
        edge = db.execute("SELECT rowid FROM dedupe_keys ORDER BY rowid DESC LIMIT 1").fetchone()
        if edge is None:
            return ObservedReviewLifecycleRegistrationPage(
                repository, (), None, None, False, 0, 0, version,
            )
        upper_rowid = edge["rowid"]
    position = "" if after_rowid is None else "AND rowid>? "
    parameters: tuple[object, ...] = (
        REVIEW_SUBMISSION_NAMESPACE, len(repository.encode()), repository, upper_rowid,
    )
    if after_rowid is not None:
        parameters = (*parameters, after_rowid)
    rows = db.execute(
        "SELECT rowid AS source_rowid,typeof(namespace) AS namespace_type,"
        "CAST(namespace AS BLOB)=CAST(? AS BLOB) AS selected,"
        "typeof(scope) AS scope_type,"
        f"CASE WHEN {_size_expression('scope')}=? "
        "THEN CAST(scope AS BLOB)=CAST(? AS BLOB) ELSE 0 END AS scoped "
        "FROM dedupe_keys WHERE rowid<=? "
        f"{position}ORDER BY rowid LIMIT ?",
        (*parameters, limit + 1),
    ).fetchall()
    sources: list[ReviewLifecycleRegistrationSource] = []
    source_bytes = 0
    scanned_rows = 0
    next_rowid = after_rowid
    sizes = tuple(_size_expression(column) for column in _DEDUPE_SOURCE_COLUMNS)
    for row in rows[:limit]:
        rowid = row["source_rowid"]
        if row["selected"]:
            if row["namespace_type"] != "text":
                return BlockedReviewLifecycleRegistrationPage(
                    "review registration namespace has a BLOB alias", rowid,
                )
            if row["scoped"]:
                if row["scope_type"] != "text":
                    return BlockedReviewLifecycleRegistrationPage(
                        "review registration repository has a BLOB alias", rowid,
                    )
                metadata = db.execute(
                    "SELECT typeof(key) AS key_type,typeof(claimed_at) AS claimed_type,"
                    "typeof(reusable_after) AS reusable_type,typeof(payload_json) AS payload_type,"
                    f"max({','.join(sizes)}) AS largest_value,"
                    f"{'+'.join(sizes)} AS source_bytes "
                    "FROM dedupe_keys WHERE rowid=?",
                    (rowid,),
                ).fetchone()
                if (metadata is None or metadata["key_type"] != "text"
                        or metadata["claimed_type"] != "text"
                        or metadata["reusable_type"] != "null"
                        or metadata["payload_type"] != "text"
                        or type(metadata["largest_value"]) is not int
                        or type(metadata["source_bytes"]) is not int):
                    return BlockedReviewLifecycleRegistrationPage(
                        "review registration has unreadable fields", rowid,
                    )
                size = metadata["source_bytes"]
                if size > max_source_bytes - source_bytes:
                    if scanned_rows:
                        break
                    return BlockedReviewLifecycleRegistrationPage(
                        "review registration exceeds the page byte bound", rowid,
                    )
                try:
                    identity = db.execute(
                        "SELECT key FROM dedupe_keys WHERE rowid=? AND namespace=? AND scope=?",
                        (rowid, REVIEW_SUBMISSION_NAMESPACE, repository),
                    ).fetchone()
                    if identity is None:
                        raise ValueError("registration identity changed during read")
                    fields, digest = _read_recording_source(
                        db, REVIEW_SUBMISSION_NAMESPACE, repository, identity["key"],
                    )
                    recorded = dict(fields)
                    registration = decode_workflow(
                        recorded["payload_json"], ReviewSubmissionRegistration,
                    )
                    claimed = datetime.fromisoformat(recorded["claimed_at"])
                    if (encode_workflow(registration) != recorded["payload_json"]
                            or registration.submission.command_id != identity["key"]
                            or registration.round.binding.rejection.fence.key.repository != repository
                            or claimed.tzinfo is None or claimed.utcoffset() is None):
                        raise ValueError("review registration disagrees with its recorded source")
                    sources.append(ReviewLifecycleRegistrationSource(
                        rowid, registration, digest, size,
                    ))
                    source_bytes += size
                except (TypeError, ValueError, UnicodeError, RecursionError,
                        sqlite3.Error, WorkflowAuthorityError) as exc:
                    return BlockedReviewLifecycleRegistrationPage(
                        f"review registration is unreadable: {str(exc)[:465]}", rowid,
                    )
        scanned_rows += 1
        next_rowid = rowid
    return ObservedReviewLifecycleRegistrationPage(
        repository, tuple(sources), next_rowid, upper_rowid,
        len(rows) > scanned_rows, scanned_rows, source_bytes, version,
    )
