"""Atomic SQL publication for a caller-revalidated review execution import."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import sqlite3

from ..domain.review_execution import (
    CurrentReviewExecution, ImportedReviewExecutionOriginReceipt,
)
from ..domain.review_execution_import_plan import EmptyReviewExecutionImportPlan
from ..domain.review_workflow import ReviewWorkflowSnapshot
from ..domain.workflow_identity import FamilyAuthority, WorkflowFamily
from .workflow_codec import encode_workflow
from .workflow_rows import (
    WorkflowAuthorityError, insert_imported_review_execution_origin,
    insert_workflow_component, load_review_workflow_row, persist_workflow_snapshot,
    workflow_scope,
)


def commit_empty_review_execution_import(
    db: sqlite3.Connection, plan: EmptyReviewExecutionImportPlan, *, now: datetime,
) -> tuple[ReviewWorkflowSnapshot, ...]:
    """Publish a fully revalidated plan inside the caller's open write transaction.

    The caller owns deployment barriers and fresh source rederivation. This
    function checks the current SQL workflow projection and time once more,
    then leaves rollback of any failed write to the enclosing transaction.
    """
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("review execution import requires an open SQL transaction")
    if not isinstance(plan, EmptyReviewExecutionImportPlan):
        raise ValueError("review execution import requires a typed plan")
    decisions = plan.decisions_at(now)
    family = WorkflowFamily.REVIEW_EXECUTION
    if db.execute(
        "SELECT 1 FROM workflow_authority_epochs WHERE repository=? AND family=?",
        (plan.repository, family.value),
    ).fetchone() is not None:
        raise WorkflowAuthorityError("review execution family is already active")
    current: list[ReviewWorkflowSnapshot] = []
    for candidate in plan.candidates:
        snapshot = load_review_workflow_row(db, candidate.workflow.revision.fence.key)
        if snapshot != candidate.workflow:
            raise WorkflowAuthorityError("review execution import workflow changed before commit")
        current.append(snapshot)

    db.execute(
        "INSERT INTO workflow_authority_epochs VALUES (?, ?, ?, ?, ?, ?)",
        (plan.repository, family.value, plan.execution_epoch, plan.source_digest,
         plan.inventory_digest, now.isoformat()),
    )
    db.execute(
        "INSERT INTO workflow_activation_evidence VALUES (?, ?, ?, ?, ?)",
        (plan.repository, family.value, plan.execution_epoch, 0, plan.plan_digest),
    )
    authority = FamilyAuthority(family, plan.execution_epoch)
    committed: list[ReviewWorkflowSnapshot] = []
    for snapshot, decision in zip(current, decisions, strict=True):
        key = snapshot.revision.fence.key
        encoded = encode_workflow(decision)
        execution = CurrentReviewExecution(
            decision.origin, decision.checkpoint, decision.checkpoint.state, 0, 0,
        )
        receipt = ImportedReviewExecutionOriginReceipt(
            decision.origin, decision.checkpoint, plan.plan_digest, plan.source_digest,
            plan.inventory_digest, hashlib.sha256(encoded.encode()).hexdigest(),
        )
        db.execute(
            "INSERT INTO workflow_activation_imports VALUES (?, ?, ?, ?, ?, ?, ?)",
            (plan.repository, family.value, plan.execution_epoch, *workflow_scope(key)[1:],
             plan.source_digest, encoded),
        )
        insert_workflow_component(db, key, authority, execution.binding.execution_episode)
        insert_imported_review_execution_origin(db, plan.repository, receipt, claimed_at=now)
        after = replace(
            snapshot,
            revision=replace(snapshot.revision, version=snapshot.revision.version + 1),
            execution=execution,
        )
        persist_workflow_snapshot(db, after)
        loaded = load_review_workflow_row(db, key)
        if loaded != after:
            raise WorkflowAuthorityError("review execution import failed canonical readback")
        committed.append(loaded)
    return tuple(committed)
