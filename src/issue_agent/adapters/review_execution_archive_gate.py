"""Small archived checkpoint certification; admission remains separate from load."""

from datetime import datetime
import sqlite3

from ..domain.review_execution import ActivatedReviewExecution
from ..domain.review_execution_archive_gate import (
    BlockedReviewExecutionArchive, CertifiedReviewExecutionArchive,
    ReviewExecutionArchiveGate, certified_review_execution_archive,
    review_execution_archive_admission_refusal,
)
from ..domain.workflow_identity import WorkflowKey
from .review_execution_activation import _read_recording_source
from .review_checkpoint_application_source import read_review_checkpoint_application_source
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, REVIEW_EXECUTION_ORIGIN_NAMESPACE,
    WorkflowAuthorityError, _unique_text_identity_rowid,
)


REVIEW_EXECUTION_ARCHIVE_GATE_NAMESPACE = "review-execution-archive-gate-v1"


def review_execution_archive_gate_key(execution: ActivatedReviewExecution) -> str:
    if not isinstance(execution, ActivatedReviewExecution):
        raise ValueError("archive gate requires active execution authority")
    binding = execution.binding
    return f"pr:{binding.rejection.fence.key.target.number}:{binding.execution_epoch}:{binding.execution_episode}"


def _read_review_execution_archive_gate_record(
    db: sqlite3.Connection, key: WorkflowKey, execution: ActivatedReviewExecution,
) -> ReviewExecutionArchiveGate:
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey)
            or not isinstance(execution, ActivatedReviewExecution)
            or key != execution.binding.rejection.fence.key):
        raise ValueError("archive gate requires the current PR execution in a transaction")
    gate_key = review_execution_archive_gate_key(execution)
    rowid = _unique_text_identity_rowid(
        db, "level_state", (("namespace", REVIEW_EXECUTION_ARCHIVE_GATE_NAMESPACE),
                            ("scope", key.repository), ("key", gate_key)),
        source="review execution archive gate",
    )
    metadata = db.execute(
        "SELECT typeof(payload_json) AS payload_type,length(CAST(payload_json AS BLOB)) AS size "
        "FROM level_state WHERE rowid=?", (rowid,),
    ).fetchone()
    if (metadata is None or metadata["payload_type"] != "text"
            or type(metadata["size"]) is not int or metadata["size"] > MAX_WORKFLOW_PAYLOAD_BYTES):
        raise WorkflowAuthorityError("review execution archive gate is absent or oversized")
    raw = db.execute("SELECT payload_json FROM level_state WHERE rowid=?", (rowid,)).fetchone()
    try:
        gate = decode_workflow(raw["payload_json"], CertifiedReviewExecutionArchive | BlockedReviewExecutionArchive)
        if encode_workflow(gate) != raw["payload_json"]:
            raise ValueError("noncanonical archive gate")
        certified_review_execution_archive(gate, execution)
        return gate
    except (TypeError, ValueError, UnicodeError, RecursionError, sqlite3.Error) as exc:
        raise WorkflowAuthorityError("review execution archive certification is unreadable or changed") from exc


def read_review_execution_archive_gate(
    db: sqlite3.Connection, key: WorkflowKey, execution: ActivatedReviewExecution,
) -> ReviewExecutionArchiveGate:
    gate = _read_review_execution_archive_gate_record(db, key, execution)
    certificate = certified_review_execution_archive(gate, execution)
    try:
        _, origin_digest = _read_recording_source(
            db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository, execution.origin.evidence_id,
        )
        _, checkpoint_digest = _read_recording_source(
            db, REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, key.repository,
            certificate.checkpoint_receipt_id,
        )
        application = read_review_checkpoint_application_source(
            db, key.repository, certificate.checkpoint_receipt_id,
        )
        if (certificate.origin_source_sha256 != origin_digest
                or certificate.checkpoint_source_sha256 != checkpoint_digest
                or certificate.application_source_sha256 != application.source_sha256
                or application.checkpoint.checkpoint != execution.checkpoint):
            raise ValueError("certified checkpoint sources changed")
        return gate
    except (TypeError, ValueError, UnicodeError, RecursionError, sqlite3.Error) as exc:
        raise WorkflowAuthorityError("review execution archive certification is unreadable or changed") from exc


def persist_review_execution_archive_gate(
    db: sqlite3.Connection, key: WorkflowKey, execution: ActivatedReviewExecution,
    gate: ReviewExecutionArchiveGate, *, now: datetime,
) -> None:
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("archive gate publication requires a transaction")
    certified_review_execution_archive(gate, execution)
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or key != execution.binding.rejection.fence.key):
        raise ValueError("archive gate publication requires matching authority and aware time")
    db.execute(
        "INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(namespace,scope,key) DO UPDATE SET payload_json=excluded.payload_json,"
        "updated_at=excluded.updated_at",
        (REVIEW_EXECUTION_ARCHIVE_GATE_NAMESPACE, key.repository,
         review_execution_archive_gate_key(execution), encode_workflow(gate), now.isoformat()),
    )


def require_review_execution_archive_admission(
    db: sqlite3.Connection, key: WorkflowKey, execution: ActivatedReviewExecution, *, now: datetime,
) -> None:
    if execution.checkpoint_receipt is None:
        return
    gate = read_review_execution_archive_gate(db, key, execution)
    refusal = review_execution_archive_admission_refusal(gate, execution, now)
    if refusal is not None:
        raise WorkflowAuthorityError(refusal)
