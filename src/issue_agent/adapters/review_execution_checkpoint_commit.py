"""Atomic source-verified checkpoint publication and durable audit certification."""

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import sqlite3

from ..domain.review_checkpoint_application import application_receipt_from_proposal
from ..domain.review_execution import ActivatedReviewExecution, ReviewExecutionCheckpointReceipt
from ..domain.review_execution_archive_gate import CertifiedReviewExecutionArchive
from ..domain.workflow_identity import WorkflowKey, WorkflowRevision
from .review_archive_source_versions import (
    read_review_archive_source_versions, read_review_lifecycle_source_version,
)
from .review_checkpoint_application_source import REVIEW_CHECKPOINT_APPLICATION_NAMESPACE
from .review_execution_activation import _read_recording_source
from .review_execution_archive_evidence import (
    observe_initial_review_execution_archive_evidence, observe_review_execution_archive_evidence,
)
from .review_execution_archive_gate import (
    persist_review_execution_archive_gate, require_review_execution_archive_admission,
)
from .review_execution_compaction import observe_review_checkpoint_manifest
from .review_archive_identity import require_review_archive_identity_audit
from .sql_read_budget import MAX_SQL_READ_BYTES, bounded_sql_source_reads
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import (
    REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, REVIEW_EXECUTION_ORIGIN_NAMESPACE,
    WorkflowAuthorityError, _read_checkpoint_receipt_row,
    load_review_workflow_row, persist_workflow_snapshot, review_execution_checkpoint_receipt_id,
)


REVIEW_CHECKPOINT_PUBLICATION_NAMESPACE = "review-checkpoint-publication"


@dataclass(frozen=True)
class ReviewCheckpointCommitted:
    receipt_id: str
    receipt: ReviewExecutionCheckpointReceipt
    idempotent: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.receipt, ReviewExecutionCheckpointReceipt)
                or self.receipt_id != review_execution_checkpoint_receipt_id(self.receipt)
                or type(self.idempotent) is not bool):
            raise ValueError("checkpoint result requires its canonical receipt")


def _publication_key(expected: WorkflowRevision, through_sequence: int) -> str:
    return hashlib.sha256(encode_workflow((expected, through_sequence)).encode()).hexdigest()


def _replay(db, key, expected, through_sequence, execution):
    publication = _publication_key(expected, through_sequence)
    row = db.execute(
        "SELECT 1 FROM dedupe_keys WHERE namespace IN (?,CAST(? AS BLOB)) "
        "AND scope IN (?,CAST(? AS BLOB)) AND key IN (?,CAST(? AS BLOB)) LIMIT 1",
        (REVIEW_CHECKPOINT_PUBLICATION_NAMESPACE, REVIEW_CHECKPOINT_PUBLICATION_NAMESPACE,
         key.repository, key.repository, publication, publication),
    ).fetchone()
    if row is None:
        return None
    fields, _ = _read_recording_source(
        db, REVIEW_CHECKPOINT_PUBLICATION_NAMESPACE, key.repository, publication,
    )
    values = dict(fields)
    claimed_at = values["claimed_at"]
    if not isinstance(claimed_at, str) or len(claimed_at.encode()) > 64:
        raise WorkflowAuthorityError("checkpoint publication has malformed claim time")
    claimed = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
    if claimed.tzinfo is None or claimed.utcoffset() is None or values["reusable_after"] is not None:
        raise WorkflowAuthorityError("checkpoint publication must be permanent with an aware claim time")
    recorded = decode_workflow(values["payload_json"], tuple)
    if (len(recorded) != 3 or recorded[:2] != (expected, through_sequence)
            or not isinstance(recorded[2], str) or encode_workflow(recorded) != values["payload_json"]):
        raise WorkflowAuthorityError("checkpoint publication identity contains conflicting facts")
    receipt, _ = _read_checkpoint_receipt_row(db, key.repository, recorded[2])
    from .review_checkpoint_application_source import read_review_checkpoint_application_source

    application = read_review_checkpoint_application_source(db, key.repository, recorded[2])
    for namespace in (REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, REVIEW_CHECKPOINT_APPLICATION_NAMESPACE):
        source, _ = _read_recording_source(db, namespace, key.repository, recorded[2])
        if dict(source)["claimed_at"] != claimed_at:
            raise WorkflowAuthorityError("checkpoint publication changed its paired claim time")
    if (application.recorded.captured_revision != expected
            or receipt.checkpoint.through_sequence != through_sequence
            or receipt.origin != execution.origin
            or receipt.checkpoint.through_sequence > execution.checkpoint.through_sequence
            or receipt.checkpoint.state.binding.rejection.fence.generation
            > execution.checkpoint.state.binding.rejection.fence.generation):
        raise WorkflowAuthorityError("checkpoint publication does not match current certified ancestry")
    return ReviewCheckpointCommitted(recorded[2], receipt, idempotent=True)


def _commit_review_execution_checkpoint(
    db: sqlite3.Connection, key: WorkflowKey, expected: WorkflowRevision,
    through_sequence: int, *, now: datetime, max_source_bytes: int = MAX_SQL_READ_BYTES,
) -> ReviewCheckpointCommitted:
    """Publish a complete proof in the caller's write transaction.

    The transaction must be BEGIN IMMEDIATE. No receipt or projection is
    published before current source censuses and full lineage have been proved.
    A source-bound exact retry precedes admission deadline checks.
    """
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(key, WorkflowKey) or not isinstance(expected, WorkflowRevision)
            or expected.fence.key != key or type(through_sequence) is not int
            or through_sequence < 0 or not isinstance(now, datetime)
            or now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("checkpoint publication requires a matching transaction, revision and aware time")
    try:
        with bounded_sql_source_reads(db, max_source_bytes):
            snapshot = load_review_workflow_row(db, key)
            if snapshot is None or not isinstance(snapshot.execution, ActivatedReviewExecution):
                raise WorkflowAuthorityError("checkpoint publication requires active execution authority")
            execution = snapshot.execution
            replay = _replay(db, key, expected, through_sequence, execution)
            if replay is not None:
                return replay
            if snapshot.revision != expected:
                raise WorkflowAuthorityError("checkpoint publication aggregate revision changed")
            require_review_execution_archive_admission(db, key, execution, now=now)
            identity_proof = require_review_archive_identity_audit(db, now=now)
            evidence = (
                observe_initial_review_execution_archive_evidence(db, key, now=now)
                if execution.checkpoint_receipt is None
                else observe_review_execution_archive_evidence(db, key, now=now)
            )
            if evidence.execution != execution:
                raise WorkflowAuthorityError("checkpoint source passes changed their current execution")
            manifest = observe_review_checkpoint_manifest(
                db, key, through_sequence, max_source_bytes=max_source_bytes, verify_lineage=True,
            )
            if manifest.revision != expected:
                raise WorkflowAuthorityError("checkpoint manifest changed its captured revision")
            receipt = manifest.receipt
            receipt_id = review_execution_checkpoint_receipt_id(receipt)
            application = application_receipt_from_proposal(receipt_id, manifest.application)
            for namespace, payload in (
                (REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, encode_workflow(receipt)),
                (REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, encode_workflow(application)),
            ):
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                    (namespace, key.repository, receipt_id, now.isoformat(), payload),
                )
            compacted = replace(
                execution, checkpoint=manifest.plan.checkpoint,
                checkpoint_receipt=receipt_id, boundaries=manifest.plan.remaining_boundaries,
            )
            after = replace(snapshot, execution=compacted,
                            revision=WorkflowRevision(expected.fence, expected.version + 1))
            persist_workflow_snapshot(db, after)
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                (REVIEW_CHECKPOINT_PUBLICATION_NAMESPACE, key.repository,
                 _publication_key(expected, through_sequence), now.isoformat(),
                 encode_workflow((expected, through_sequence, receipt_id))),
            )
            _, origin_digest = _read_recording_source(
                db, REVIEW_EXECUTION_ORIGIN_NAMESPACE, key.repository, compacted.origin.evidence_id,
            )
            _, checkpoint_digest = _read_recording_source(
                db, REVIEW_EXECUTION_CHECKPOINT_NAMESPACE, key.repository, receipt_id,
            )
            _, application_digest = _read_recording_source(
                db, REVIEW_CHECKPOINT_APPLICATION_NAMESPACE, key.repository, receipt_id,
            )
            due = min(evidence.rounds.next_due_at, evidence.receipts.next_due_at,
                      evidence.generations.next_due_at, evidence.lifecycle.next_due_at,
                      identity_proof.next_due_at)
            if hasattr(evidence, "checkpoints"):
                due = min(due, evidence.checkpoints.next_due_at)
            else:
                due = min(due, evidence.checkpoint_absence.next_due_at)
            gate = CertifiedReviewExecutionArchive(
                compacted.origin, evidence.rounds.origin, receipt_id, compacted.checkpoint,
                origin_digest, checkpoint_digest, application_digest,
                read_review_archive_source_versions(db, key.repository),
                read_review_lifecycle_source_version(db), now, due,
            )
            persist_review_execution_archive_gate(db, key, compacted, gate, now=now)
            if load_review_workflow_row(db, key) != after:
                raise WorkflowAuthorityError("checkpoint publication failed canonical readback")
            return ReviewCheckpointCommitted(receipt_id, receipt)
    except (TypeError, ValueError, KeyError, UnicodeError, RecursionError, sqlite3.Error) as exc:
        if isinstance(exc, WorkflowAuthorityError):
            raise
        raise WorkflowAuthorityError("checkpoint source proof or publication failed") from exc


def commit_review_execution_checkpoint(
    db: sqlite3.Connection, key: WorkflowKey, expected: WorkflowRevision,
    through_sequence: int, *, now: datetime, max_source_bytes: int = MAX_SQL_READ_BYTES,
) -> ReviewCheckpointCommitted:
    """Publish atomically even when the caller handles a refusal in its transaction."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("checkpoint publication requires an explicit caller transaction")
    db.execute("SAVEPOINT review_checkpoint_publication")
    try:
        result = _commit_review_execution_checkpoint(
            db, key, expected, through_sequence, now=now, max_source_bytes=max_source_bytes,
        )
    except BaseException:
        db.execute("ROLLBACK TO review_checkpoint_publication")
        db.execute("RELEASE review_checkpoint_publication")
        raise
    db.execute("RELEASE review_checkpoint_publication")
    return result
