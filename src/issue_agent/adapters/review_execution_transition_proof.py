"""Hot boundary proof; full accepted payload verification belongs to the audit."""

import hashlib
import sqlite3

from ..domain.review_execution import (
    ReviewExecutionGenerationBoundary, ReviewExecutionTransitionProofSource,
)
from ..domain.workflow import WorkflowMutation
from .review_execution_activation import _read_recording_source
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import WorkflowAuthorityError, _unique_text_identity_rowid


def read_review_execution_transition_proof(
    db: sqlite3.Connection, repository: str, boundary: ReviewExecutionGenerationBoundary,
    *, expected_receipt_sha256: str | None = None,
) -> ReviewExecutionTransitionProofSource:
    fields, digest = _read_recording_source(
        db, "review-execution-generation", repository, boundary.receipt_id,
    )
    values = dict(fields)
    try:
        if (values["reusable_after"] is not None
                or (expected_receipt_sha256 is not None and digest != expected_receipt_sha256)):
            raise ValueError("generation source changed")
        proof = decode_workflow(values["payload_json"], ReviewExecutionTransitionProofSource)
        if proof.boundary != boundary or encode_workflow(proof) != values["payload_json"]:
            raise ValueError("generation proof changed")
        if boundary.mutation in (WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM):
            if proof.namespace != "workflow-request":
                raise ValueError("generation request source changed")
        elif (proof.namespace != "workflow-transition" or proof.key != hashlib.sha256(
            encode_workflow(boundary.from_revision).encode(),
        ).hexdigest()):
            raise ValueError("generation transition source changed")
        rowid = _unique_text_identity_rowid(
            db, "dedupe_keys", (("namespace", proof.namespace), ("scope", repository), ("key", proof.key)),
            source="accepted review generation transition",
        )
        row = db.execute(
            "SELECT typeof(payload_json) AS payload_type,octet_length(payload_json) AS payload_bytes,"
            "typeof(claimed_at) AS claimed_type,octet_length(claimed_at) AS claimed_bytes,"
            "reusable_after FROM dedupe_keys WHERE rowid=?", (rowid,),
        ).fetchone()
        if (row is None or row["payload_type"] != "text" or row["claimed_type"] != "text"
                or type(row["payload_bytes"]) is not int
                or not 1 <= row["payload_bytes"] <= MAX_WORKFLOW_PAYLOAD_BYTES
                or type(row["claimed_bytes"]) is not int or not 1 <= row["claimed_bytes"] <= 64
                or row["reusable_after"] is not None):
            raise ValueError("accepted generation source is absent or malformed")
        return proof
    except (TypeError, ValueError, KeyError, UnicodeError, RecursionError, sqlite3.Error) as exc:
        raise WorkflowAuthorityError("review generation lacks a bounded transition proof") from exc
